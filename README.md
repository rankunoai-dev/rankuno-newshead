# The RankUno Brief

Collects search, AI and marketing news every day and sends RankUno staff a formal email digest
twice a week (Monday and Thursday, 09:00 IST).

- **Daily fetch (no AI):** RSS feeds from official blogs, industry publications and Reddit.
- **Scoring:** keyword rules decide each story's section and importance.
- **Issue build:** duplicates are merged, limits applied, and an Outlook-safe email is rendered.
- **Send:** each recipient's delivery is recorded, so re-running a send never emails anyone twice.

## Setup (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env      # then fill in the SMTP values
```

## Commands

```powershell
.\.venv\Scripts\python.exe -m rankuno_brief fetch            # daily: download feeds, store new items
.\.venv\Scripts\python.exe -m rankuno_brief build            # assemble the next issue
.\.venv\Scripts\python.exe -m rankuno_brief send --test      # [TEST] copy to the configured recipients
.\.venv\Scripts\python.exe -m rankuno_brief send             # real send (recorded; never sends twice)
.\.venv\Scripts\python.exe -m rankuno_brief sources          # health of every source
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
| `config/settings.yaml` | Name, schedule, issue limits, fetch behaviour, recipients |
| `config/sources.yaml` | News feeds: weight, default section, keyword requirement |
| `config/taxonomy.yaml` | Newsletter sections and the keywords that route stories into them |
| `.env` | Email credentials (never committed) |

To add a source, add an entry to `sources.yaml`. No code changes are needed.

## Project status

**Phase 1 is done:** a minimal version that works end to end.

- 19 live feeds, daily fetch with retries and source isolation.
- Rule-based scoring and sections.
- Formal Outlook-safe template with embedded logos.
- Delivery that never sends anyone the same issue twice.

**Coming next:**

| Phase | Adds |
|---|---|
| 2 | Google News / Alerts keyword feeds, staff tips mailbox, auto-pausing broken sources, source health report |
| 3 | Full article text, tool and resource link extraction, grouping of same-story coverage that uses different wording |
| 4 | Low-cost batched AI summaries and "why it matters" on selected stories only, with fallback when the AI is down |
| 5 | Template refinements from stakeholder feedback, tested in Outlook desktop, Outlook web and mobile |
| 6 | Hosting, scheduler (06:00 fetch; Mon/Thu 09:00 send), Microsoft Graph sending, monitoring, backups |

**Known limitations in Phase 1:**

- **Near-duplicate stories:** the same story reworded by different publishers is not always merged (Phase 3).
- **Summaries** are the publishers' own excerpts (Phase 4).
- **AdExchanger** is disabled: it blocks Python clients (see `sources.yaml`).
