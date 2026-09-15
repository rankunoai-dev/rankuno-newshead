# The RankUno Brief

Collects search, AI and marketing news every day and sends RankUno staff a formal email digest
twice a week (Monday and Thursday, 09:00 IST).

- **Daily fetch (no AI):** publishers' RSS feeds, Google News searches, Techmeme, Hacker News, Reddit,
  and Quora through Google Alerts.
- **Scoring:** keyword rules decide each story's section and importance.
- **Quality filters:** press-release wires are dropped. Stories found only through Google News need a
  trusted publisher or coverage by at least two publishers.
- **Issue build:** duplicates are merged under the most trusted version. Google News links are
  resolved to the original articles, and missing images and summaries are read from the article
  pages. An Outlook-safe email is rendered.
- **Security layer:** every story is screened for offensive or sensitive content before it can be
  selected, the finished email is checked again before it is saved and before it is sent, and only
  allowed, valid addresses receive it. See [Security layer](#security-layer).
- **Send:** each recipient's delivery is recorded, so re-running a send never emails anyone twice.

## Setup (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env      # then fill in the SMTP values
```

## Commands

```powershell
.\.venv\Scripts\python.exe -m rankuno_brief fetch            # daily: download all sources, store new items
.\.venv\Scripts\python.exe -m rankuno_brief build            # assemble the next issue
.\.venv\Scripts\python.exe -m rankuno_brief build --offline  # same, without web lookups (links, images)
.\.venv\Scripts\python.exe -m rankuno_brief send --test      # [TEST] copy to the configured recipients
.\.venv\Scripts\python.exe -m rankuno_brief send             # real send (recorded; never sends twice)
.\.venv\Scripts\python.exe -m rankuno_brief sources          # health of every source
.\.venv\Scripts\python.exe -m rankuno_brief security check   # every pre-send check, without sending
.\.venv\Scripts\python.exe -m rankuno_brief security review  # stories held or blocked by the content screen
.\.venv\Scripts\python.exe -m pytest                         # run the tests
```

`build` writes these files to `data/issues/<date>/`:

| File | Use |
|---|---|
| `preview.html` | Open in a browser |
| `email.eml` | Double-click to see it in Outlook |
| `email.html` / `email.txt` | The bodies that get sent |

## Configuration

| File | What it controls |
|---|---|
| `config/settings.yaml` | Name, schedule, issue limits, fetch behaviour, where the recipient list lives |
| `config/sources.yaml` | Sources (type, weight, default section, filters), excluded and trusted publishers |
| `config/taxonomy.yaml` | Newsletter sections and the keywords that route stories into them |
| `config/recipients.txt` | Who receives the brief: one address per line, or a list pasted from Outlook |
| `config/security.yaml` | Allowed recipient domains and addresses, sending limits and pace, unsubscribe header |
| `config/content_filter.yaml` | Blocked and held terms, harmless exceptions, blocked sites and link shorteners |
| `.env` | Email credentials (never committed) |

To add a source, add an entry to `sources.yaml`. No code changes are needed.

### Source types

| Type | Example | Shown in the email as |
|---|---|---|
| `rss` | Search Engine Land | The publisher |
| `google_news` | A search query such as `"AI Overviews" OR "AI Mode"` | Original publisher · Found via Google News |
| `techmeme` | Techmeme | Original publisher (or `@handle on X`) · Found via Techmeme |
| `hackernews` | Popular stories above `min_points` | Original site · Found via Hacker News (thread linked) |
| `reddit` | r/SEO+bigseo+PPC+marketing | Original site · Found via Reddit r/SEO (thread linked) |
| `google_alerts` | Quora questions | quora.com · Found via Google Alerts |

### Adding Quora through Google Alerts

Google Alerts has no API, so each alert is created by hand:

1. Sign in to https://www.google.com/alerts as rankuno@gmail.com.
2. Enter a query, e.g. `site:quora.com "AI Overviews" OR "AI Mode" OR "generative engine optimization"`.
3. Open **Show options** and set:
   - How often: *As-it-happens*
   - Sources: *Automatic*
   - Language: *English*
   - Region: *Any Region*
   - How many: *All results*
   - Deliver to: **RSS feed**
4. Click **Create alert**, then copy the RSS icon's link next to the alert.
5. In `sources.yaml`, paste the link as the `url` of `quora-ai-search` or `quora-seo-paid` and set `enabled: true`.

## Security layer

Nothing reaches an inbox without passing four gates (code in `rankuno_brief/security/`):

| Gate | When | What it does |
|---|---|---|
| Content screen | `build`, before selection and again on the final stories | Checks headlines, summaries, publisher names and links against `content_filter.yaml`. Catches disguised spellings (`f*ck`, `sh1t`, `f u c k`, look-alike letters, hidden characters). Drops unsafe links (shorteners, raw IPs, blocked sites) and unsafe images (plain http, community posts). Tidies headlines that read as spam (ALL CAPS, `!!!`, emoji). |
| Output gate | `build`, before any file is written; `send`, again | The finished email must contain no blocked term, script, form, event handler or unsafe link. Each build records a fingerprint of its files; `send` refuses files edited afterwards. |
| Recipients | `send` | Only well-formed addresses on `allowed_domains` or `allowed_addresses`. Mistyped domains (with a "did you mean"), domains without a mail server and suppressed addresses are skipped and reported. The send is refused if the list exceeds `max_recipients`. |
| Deliverability | `send` | Checks subject, headers, links, images, size and the plain-text version for spam signals, and SPF, DKIM and DMARC for the sender's domain. Adds a List-Unsubscribe header, sends one message per recipient, spaces messages out, stops if the server rejects the message itself, and suppresses addresses that keep bouncing. |

Errors stop the send; warnings are reported. `security check` shows the full report at any time.

**Blocked vs held.** Blocked terms (profanity, slurs, explicit sexual language, graphic violence) are
never published. Held topics (politics, crime, self-harm, adult themes, gambling, spam phrases, and
more) are left out until an editor approves the story:

```powershell
.\.venv\Scripts\python.exe -m rankuno_brief security review          # list held and blocked stories
.\.venv\Scripts\python.exe -m rankuno_brief security approve 742     # allow a held story; then build again
.\.venv\Scripts\python.exe -m rankuno_brief security revoke 742      # undo an approval
.\.venv\Scripts\python.exe -m rankuno_brief security scan "headline" # test text or a link against the filter
.\.venv\Scripts\python.exe -m rankuno_brief security unsuppress someone@rankuno.com
```

A blocked false positive is fixed by adding the harmless phrase to `allow_phrases` in `content_filter.yaml`.

**Adding recipients.** Paste the addresses into `config/recipients.txt`, then run `security check`
to confirm every address is accepted before the next send.

## Project status

**Phase 1 is done:** a minimal version that works end to end.

- Daily fetch with retries and source isolation.
- Rule-based scoring and sections.
- Formal Outlook-safe template with embedded logos.
- Delivery that never sends anyone the same issue twice.

**Phase 2 (sources) is done:**

- 37 active sources.
  - Added: Google Search and Bing blogs, Meta Newsroom, Moz, Lily Ray, Marie Haynes, TechCrunch AI, Ars Technica AI, Wired AI, Adweek.
  - New source types: Techmeme, five Google News searches, Hacker News.
- Google News links resolved to the original articles, and press-release filtering.
- Trusted-publisher and coverage rules.
- Images and summaries read from article pages when a source doesn't supply them.
- Quora via Google Alerts is ready and needs the alerts created (above).

**Security layer is done:** content screening with editor approval, output gate with tamper check,
recipient allowlist and bounce suppression, spam-signal and SPF/DKIM/DMARC checks (above).

**Coming next:**

| Phase | Adds |
|---|---|
| 2 (remaining) | Staff tips mailbox, auto-pausing sources that keep failing |
| 3 | Full article text, tool and resource link extraction, grouping of same-story coverage that uses different wording |
| 4 | Low-cost batched AI summaries and "why it matters" on selected stories only, with fallback when the AI is down |
| 5 | Template refinements from stakeholder feedback, tested in Outlook desktop, Outlook web and mobile |
| 6 | Hosting, scheduler (06:00 fetch; Mon/Thu 09:00 send), Microsoft Graph sending, monitoring, backups |

**Known limitations:**

- **Near-duplicate stories:** the same story reworded very differently (e.g. "advisers" vs "advisors") is not always merged (Phase 3).
- **Summaries** are the publishers' own excerpts or page descriptions (Phase 4).
- **Google News link resolution** uses an unofficial Google endpoint. If it stops working, stories keep their Google News links, which still open the article.
- **Not reachable** as of 2026-09-15 (disabled in `sources.yaml`):
  - AdExchanger blocks Python clients. Its stories still arrive via Google News.
  - VentureBeat rate-limits every request.
- **X/Twitter** is not fetched directly (the paid API isn't approved). Posts cited by Techmeme still appear.
- **Content screening is rule-based.** It reads words, not meaning, so an offensive story written in
  entirely polite words, or an offensive picture on an otherwise clean article, is not detected.
  Images from community sources are never shown for that reason.
- **Inbox placement also depends on DNS.** rankuno.com has no DMARC record yet (`security check` shows this).
