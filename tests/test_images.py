import email
import io
import socket
from datetime import date, timedelta
from email import policy

import httpx
import pytest
from conftest import NOW, make_item
from PIL import Image

from rankuno_brief import compose, render
from rankuno_brief.images import (
    LEAD_WIDTH,
    THUMB_WIDTH,
    ImageRejected,
    download_image,
    embed_story_images,
    is_public_https,
    to_email_jpeg,
)
from rankuno_brief.mailer import build_message, mime_bytes
from rankuno_brief.security.preflight import html_fingerprint, sha256


def picture(width, height, fmt, mode="RGB", color=(200, 30, 40)):
    out = io.BytesIO()
    Image.new(mode, (width, height), color).save(out, fmt)
    return out.getvalue()


def public(host, port, proto=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", ("93.184.215.14", port))]


# Conversion -------------------------------------------------------------------------------------


def test_webp_becomes_a_jpeg_sized_for_the_thumbnail():
    jpeg, size = to_email_jpeg(picture(1600, 900, "WEBP"), THUMB_WIDTH)
    assert jpeg[:3] == b"\xff\xd8\xff"
    assert size == (300, 169) and len(jpeg) < 30_000


def test_transparent_png_is_flattened_onto_white():
    jpeg, _ = to_email_jpeg(picture(800, 400, "PNG", mode="RGBA", color=(0, 0, 0, 0)), THUMB_WIDTH)
    assert Image.open(io.BytesIO(jpeg)).getpixel((10, 10))[0] > 240


def test_tall_infographic_is_cropped_and_small_image_is_not_enlarged():
    _, tall = to_email_jpeg(picture(600, 6000, "JPEG"), LEAD_WIDTH)
    assert tall == (600, 900)
    _, small = to_email_jpeg(picture(500, 300, "JPEG"), LEAD_WIDTH)
    assert small == (500, 300)


@pytest.mark.parametrize(
    ("data", "display_width"),
    [
        (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", THUMB_WIDTH),
        (b"not an image at all", THUMB_WIDTH),
        (picture(64, 64, "PNG"), THUMB_WIDTH),  # icon-sized
        (picture(300, 200, "JPEG"), LEAD_WIDTH),  # too small to lead the issue
        (picture(9000, 9000, "PNG", mode="1", color=1), THUMB_WIDTH),  # 81 million pixels
    ],
    ids=["svg", "not-an-image", "icon", "too-small-to-lead", "decompression-bomb"],
)
def test_unusable_or_dangerous_images_are_rejected(data, display_width):
    with pytest.raises(ImageRejected):
        to_email_jpeg(data, display_width)


# Downloads --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "address"),
    [
        ("http://example.com/a.jpg", "93.184.215.14"),
        ("https://localhost/a.jpg", "127.0.0.1"),
        ("https://internal.example/a.jpg", "10.0.0.5"),
        ("https://metadata.example/latest", "169.254.169.254"),
        ("https://v6.example/a.jpg", "::1"),
    ],
)
def test_only_public_https_addresses_are_fetched(url, address):
    def resolve(host, port, proto=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", (address, port))]

    assert not is_public_https(url, resolve)


def test_redirect_to_a_private_address_is_not_followed():
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://internal.example/secret.jpg"})

    def resolve(host, port, proto=0):
        address = "10.0.0.5" if host == "internal.example" else "93.184.215.14"
        return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", (address, port))]

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert download_image(client, "https://images.example/a.jpg", resolve) is None


def test_story_images_are_embedded_and_failures_left_out(cfg, tmp_path):
    rows = [
        make_item(1, "AI Overviews expand to 40 more countries", "search-engine-land", image="https://img.example/lead.png"),
        make_item(2, "Google Ads adds Performance Max reports", "ppc-land", image="https://img.example/thumb.webp"),
        make_item(3, "Search Console adds new Insights report", "search-engine-journal", image="https://img.example/missing.jpg"),
    ]
    content = compose.build_content(rows, cfg, NOW)
    served = {"/lead.png": picture(1600, 900, "PNG"), "/thumb.webp": picture(1200, 800, "WEBP")}

    def handler(request):
        body = served.get(request.url.path)
        return httpx.Response(200, content=body) if body else httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    lead = content.top_stories[0].item_id
    embedded = embed_story_images(content.stories, lead, tmp_path, "test", client=client, resolve=public)

    assert set(embedded) == {1, 2}
    assert embedded[lead].pixel_width == LEAD_WIDTH * 2
    other = next(image for item_id, image in embedded.items() if item_id != lead)
    assert other.pixel_width == THUMB_WIDTH * 2

    meta = render.IssueMeta(number=1, issue_date=date(2026, 9, 17), window_start=NOW - timedelta(days=3),
                            window_end=NOW, subject="S")
    html_body, _ = render.render_issue(content, meta, cfg, story_images=embedded)
    assert 'src="cid:story-1"' in html_body and 'src="cid:story-2"' in html_body
    assert "img.example" not in html_body  # nothing is linked any more
    preview, _ = render.render_issue(content, meta, cfg, preview=True, story_images=embedded)
    assert "cid:story" not in preview and "story-1.jpg" in preview

    inline = {**render.inline_images(cfg), **{image.cid: image.path for image in embedded.values()}}
    message = build_message(subject="S", html_body=html_body, text_body="t", sender="brief@rankuno.com",
                            sender_name="", recipient="rajat.singh@rankuno.com", inline_images=inline)
    parsed = email.message_from_bytes(mime_bytes(message), policy=policy.default)
    parts = {part["Content-ID"]: part["Content-Disposition"] for part in parsed.walk() if part["Content-ID"]}
    assert set(parts) == {"<logo>", "<logo_white>", "<story-1>", "<story-2>"}
    assert all(disposition.startswith("inline") for disposition in parts.values())


def test_fingerprint_covers_embedded_images():
    html = '<img src="cid:story-1">'
    assert html_fingerprint(html) == sha256(html)  # issues without pictures keep their fingerprint
    original = html_fingerprint(html, {"story-1": b"picture"})
    assert html_fingerprint(html, {"story-1": b"swapped picture"}) != original
