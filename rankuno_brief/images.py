"""Story images are downloaded when an issue is built, converted and embedded in the email instead of linked.

Linked images fail in Outlook for reasons outside our control: many sites send WebP (even at .png or .jpg
addresses), which classic Outlook for Windows cannot display; some originals are several megabytes; and
Outlook asks before downloading any linked picture. Embedded JPEGs sized for the layout always show.

The addresses come from third-party feeds, so downloads are restricted: https only, public hosts only (never
private, internal or cloud-metadata addresses, re-checked on every redirect), limited size and pixel count,
and only common image formats are decoded.
"""

from __future__ import annotations

import io
import ipaddress
import logging
import socket
import warnings
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, ImageOps

log = logging.getLogger(__name__)

LEAD_WIDTH, THUMB_WIDTH = 568, 150  # display widths used by the email template
PIXEL_RATIO = 2  # pixels per display pixel, so images stay sharp on high-resolution screens
MIN_SOURCE_WIDTH = {LEAD_WIDTH: 400, THUMB_WIDTH: 120}  # smaller pictures are usually logos or icons
MAX_DOWNLOAD_BYTES = 8_000_000
MAX_PIXELS = 40_000_000
MAX_HEIGHT_RATIO = 1.5  # taller images (infographics) are cropped from the top
MAX_REDIRECTS = 3
JPEG_QUALITY = 72
DECODABLE_FORMATS = ("JPEG", "PNG", "GIF", "WEBP", "AVIF")
ACCEPT = "image/jpeg,image/png,image/gif;q=0.9,image/webp;q=0.8,image/avif;q=0.7,*/*;q=0.1"

Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class ImageRejected(Exception):
    """The download is not an image we can safely show."""


@dataclass(frozen=True)
class EmbeddedImage:
    cid: str  # Content-ID in the email: <img src="cid:story-123">
    path: Path
    pixel_width: int
    pixel_height: int

    def display_height(self, display_width: int) -> int:
        return max(1, round(display_width * self.pixel_height / self.pixel_width))


def embed_story_images(
    stories: Iterable,
    lead_item_id: int | None,
    out_dir: Path,
    user_agent: str,
    *,
    client: httpx.Client | None = None,
    resolve: Callable = socket.getaddrinfo,
    timeout: float = 10.0,
) -> dict[int, EmbeddedImage]:
    """Download, convert and save each story's image. Returns {item_id: EmbeddedImage} for those that worked."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("story-*.jpg"):
        stale.unlink()
    wanted = [story for story in stories if story.image_url]
    if not wanted:
        return {}

    own_client = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=False, headers={"User-Agent": user_agent})
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            downloads = list(pool.map(lambda story: download_image(client, story.image_url, resolve), wanted))
    finally:
        if own_client:
            client.close()

    embedded: dict[int, EmbeddedImage] = {}
    for story, data in zip(wanted, downloads):
        if data is None:
            continue
        display_width = LEAD_WIDTH if story.item_id == lead_item_id else THUMB_WIDTH
        try:
            jpeg, (width, height) = to_email_jpeg(data, display_width)
        except ImageRejected as exc:
            log.info("Image left out for story %s (%s): %s", story.item_id, exc, story.image_url)
            continue
        cid = f"story-{story.item_id}"
        path = out_dir / f"{cid}.jpg"
        path.write_bytes(jpeg)
        embedded[story.item_id] = EmbeddedImage(cid=cid, path=path, pixel_width=width, pixel_height=height)
    log.info("Embedded %d of %d story images (%d KB)", len(embedded), len(wanted), sum(image.path.stat().st_size for image in embedded.values()) // 1000)
    return embedded


def download_image(client: httpx.Client, url: str, resolve: Callable = socket.getaddrinfo) -> bytes | None:
    for _ in range(MAX_REDIRECTS + 1):
        if not is_public_https(url, resolve):
            log.info("Image address refused (not a public https address): %s", url)
            return None
        try:
            with client.stream("GET", url, headers={"Accept": ACCEPT}) as response:
                if response.is_redirect and response.headers.get("location"):
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code != 200 or int(response.headers.get("content-length") or 0) > MAX_DOWNLOAD_BYTES:
                    log.info("Image not downloaded (HTTP %s): %s", response.status_code, url)
                    return None
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_DOWNLOAD_BYTES:
                        log.info("Image larger than %d MB, left out: %s", MAX_DOWNLOAD_BYTES // 1_000_000, url)
                        return None
                return bytes(data)
        except httpx.HTTPError as exc:
            log.info("Image not downloaded (%s): %s", type(exc).__name__, url)
            return None
    return None


def is_public_https(url: str, resolve: Callable = socket.getaddrinfo) -> bool:
    """https, and every address the host resolves to is on the public internet."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme != "https" or not host:
        return False
    try:
        addresses = {info[4][0] for info in resolve(host, parts.port or 443, proto=socket.IPPROTO_TCP)}
    except (OSError, UnicodeError):
        return False
    try:
        return bool(addresses) and all(ipaddress.ip_address(address.split("%")[0]).is_global for address in addresses)
    except ValueError:
        return False


def to_email_jpeg(data: bytes, display_width: int) -> tuple[bytes, tuple[int, int]]:
    """A JPEG sized for its place in the layout (never enlarged), with transparency flattened onto white."""
    target_width = display_width * PIXEL_RATIO
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data), formats=DECODABLE_FORMATS)
            image.draft("RGB", (target_width, target_width * 2))  # JPEG: decode at a reduced size, much faster
            image = ImageOps.exif_transpose(image)
            image.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageRejected("too many pixels") from exc
    except (OSError, ValueError, SyntaxError, Image.UnidentifiedImageError) as exc:
        raise ImageRejected("not a supported image") from exc

    if image.width < MIN_SOURCE_WIDTH[display_width]:
        raise ImageRejected(f"too small ({image.width}px wide)")
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        image = Image.new("RGB", rgba.size, "white")
        image.paste(rgba, mask=rgba.getchannel("A"))
    else:
        image = image.convert("RGB")

    max_height = int(image.width * MAX_HEIGHT_RATIO)
    if image.height > max_height:
        image = image.crop((0, 0, image.width, max_height))
    if image.width > target_width:
        image = image.resize((target_width, max(1, round(image.height * target_width / image.width))), Image.LANCZOS)

    out = io.BytesIO()
    image.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    return out.getvalue(), image.size
