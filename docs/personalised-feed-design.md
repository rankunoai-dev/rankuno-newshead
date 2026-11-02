# Personalised client feed: design

Status: design only, nothing built. Decisions still open are listed in section 12.

## 1. Goal

The existing newsletter ("The RankUno Brief") stays exactly as it is: one general digest for all staff, Mon and Thu 09:00.

A second, separate product is added: a **weekly personalised feed per client**, sent on Monday. Each feed covers one client's operating industries, competitors, sources and keywords. Account managers set it up through a form. Admins invite the managers. Managers can add team members who receive the feed.

Example: GEP (supply chain and procurement software). The GEP feed covers supply chain and procurement software news, GEP's competitors, and GEP keywords.

## 2. Roles

| Role | Who | What they can do | How they authenticate |
|---|---|---|---|
| Admin | rajat.singh@, naeem.gari@, prithviraj.pednekar@ (rankuno.com) | Invite managers, see every profile, edit or pause any profile, reassign clients, see delivery status, manage the admin list | Admin page on the existing token. Each admin should get a personal sign-in, not the shared token (see section 8). |
| Manager | Account managers who handle clients | Create and edit their own client profiles, add and remove team recipients, preview their feed, request a test send | Magic link from an admin invite. No password. A session cookie lasts 14 days. |
| Team member | Anyone a manager adds | Receives the weekly feed; can unsubscribe | None. Receives email only. |

Only admins can invite managers. Managers cannot invite other managers.

## 3. Flows

### 3.1 Admin invites a manager
1. Admin opens the admin page, enters the manager's email (rankuno.com only), and clicks Invite.
2. The system creates an invite row: a random token (only its SHA-256 hash is stored), the email, the creator, and an expiry of 7 days.
3. The system emails the invite link from the newsletter sender (Graph). The link opens a page that shows the manager's name and the form (a GET request). Nothing is consumed by the GET. Email link scanners, such as Outlook Safe Links, prefetch links, so the GET must never change state.
4. The manager submits a POST, which creates a session. The invite is marked used. The manager can come back later with a fresh link: "Request a new link" (rate-limited) or an admin resends the invite.

### 3.2 Manager fills the form (one client)
Form sections are described in section 4. The manager can save a draft at any point and submit when ready. Submitting marks the profile **active** for the next Monday.

### 3.3 Manager manages team recipients
Recipients are added in the form, or in bulk by pasting a list. Each address is validated (section 9). A recipient can receive several clients' feeds, and they get one email per client by default (see section 12, decision on combining).

### 3.4 Weekly run
See section 6.

### 3.5 Unsubscribe
Every personalised email carries a List-Unsubscribe header and a footer link. The link goes to a per-recipient, per-client token. It removes that recipient from that client only. It is not a global unsubscribe.

## 4. The form

Required: client name, operating industries / LOB. Everything else is optional. Blank optional sections are filled automatically (section 5), and the manager sees and confirms the filled values before the first send.

| Section | Fields | Required | Notes |
|---|---|---|---|
| Client | Client name | yes | Free text, 2–120 chars. Also: client website (optional, but strongly recommended for auto-fill and disambiguation). |
| Client | Aliases / other names | no | e.g. "GEP Worldwide", "GEP Software". Matching uses these too. |
| Industries / LOB | Operating industries | yes | Multi-select from a controlled list (taxonomy), plus free text for anything missing. Taxonomy values map to source groups and keyword sets. |
| Sources | Preferred sources | no | Checklist of the engine's existing sources, grouped by type (trade press, Google News, Reddit, etc.). Plus up to 10 custom RSS/Atom URLs, validated (section 9). |
| Competitors | Competitor name + website | no | Up to 15. Name required, website optional but used for disambiguation. |
| Keywords | Include keywords | no | Up to 30 phrases. Matched as whole phrases. |
| Keywords | Exclude keywords | no | Up to 20. Removes false positives, e.g. "GEP" as a gene-expression term. |
| Team | Recipient emails | no | Up to 50 per client. Validated, deduplicated, and checked against the recipient allowlist. |
| Delivery | Send Monday | fixed | Shown but not editable in v1. |
| Review | Auto-filled values | shown | Any value the engine filled is marked "suggested", with a confirm/edit control. |

Form behaviour:
- Client and industries are validated before anything else. The rest can be empty.
- Autosave every field change as a draft; drafts are per profile version.
- Editing an active profile creates a new version. The next weekly run uses the latest submitted version, never a draft.
- Every form page shows the current state: draft, active, paused.

## 5. Auto-fill when optional fields are blank

When a client profile is submitted with blank optional fields, the engine fills them. Each filled value is labelled `suggested` and stored with its source (website, LOB default, derived from competitor sites, etc.).

Filling logic, in order:
1. **Website**: if a website is given, read the homepage and its title, meta description, and main nav. Extract candidate industry terms and product names.
2. **LOB defaults**: each selected industry maps (via taxonomy.yaml) to a default source group and default keyword set. This is deterministic and free.
3. **Competitors**: derive candidates from the LOB's default competitor list (maintained by admins) and from the client's website (for example "alternatives" pages or comparison pages). Candidates are suggestions only; they are never auto-activated for matching without confirmation.
4. **Keywords**: take the client name, aliases, and the top terms from the website title and description, minus stop words.
5. **Sources**: the LOB default source group.

Two options for step 3, and step 1 can use an LLM:
- **Rules-only** (recommended for v1): steps 1–5 with no LLM. Zero marginal cost, deterministic, easy to test. Lower quality on competitors.
- **LLM-assisted**: one call per client profile, run once at submit time and again only when the manager asks for "refresh suggestions". Input is capped (for example 4 KB of website text). The output must be a strict JSON schema, and it is treated as untrusted (section 9).

Edge cases in auto-fill are listed in section 10.

## 6. Feed generation and schedule

### 6.1 What gets matched
- Items come from the **same daily fetch** that already feeds the general newsletter. The fetch is shared, not repeated per client.
- Client-specific sources (custom RSS, Google News queries for competitors and keywords) add extra fetches, described in section 11.
- Each client scores items from the window (last 7 days, plus items published up to 24 h before the window, as the general issue does):
  - Match on client name and aliases: high weight. Match on a competitor name: high weight. Match on an include keyword: medium. Match on an LOB keyword from taxonomy: low to medium.
  - Any exclude keyword: drop the item.
  - Items from the general issue are not excluded. The same story can appear in both products. Clients are not told it's the same story.
- Matching uses whole-word, case-insensitive matching, with aliases and phrase boundaries. Short names (for example "GEP", "Ariba") need an extra rule: they only match when at least one LOB term also appears, or the name appears in the title. This is the main false-positive control.
- Each client's feed is capped (for example 10 stories) and must have at least 1 story to send.
- Per-client story selection reuses the existing scoring and security layer (content screen and output gate run on every personalised issue too).

### 6.2 Per-client summaries
- Summaries are per **item**, not per client. An item summarised once for the general issue is reused for every client that matches it.
- Clients that match the same item see the same summary. Only the ordering and the "why this matters for [client]" line differ. If that line is wanted, it needs its own LLM call per (client, item) pair, so keep it off in v1 or limit it to the top 3 stories per client.

### 6.3 Schedule
- Existing: fetch daily at 06:00 IST, general issue Mon/Thu 09:00.
- New: personalised run every **Monday 10:00 IST** (after the general Monday issue so the two don't compete in the same minute). Configurable.
- The run builds all active client feeds, then sends each one to its team, paced by the existing delivery pacing (30 messages/minute on Microsoft 365).
- The catch-up window works the same way: a Monday run missed during downtime still runs within 3 hours.
- A feed is built once per run. A failed send to one recipient does not stop the others (same as the general newsletter).

### 6.4 Window and first run
- Each weekly feed covers the 7 days before the run.
- A profile activated on Saturday runs on the next Monday, with a window starting from its activation date (not 7 days back), so it doesn't dump last month's news.
- A profile activated between Monday 10:00 and the next Monday is first sent the Monday after that.

## 7. Data model (new tables, in the existing SQLite database)

```
manager_invites      id, email, token_hash, created_by, created_at, expires_at, used_at, revoked_at
managers             id, email, name, role (admin|manager), active, created_at
client_profiles      id, manager_id, client_name, status (draft|active|paused|archived), created_at, updated_at
client_profile_versions  id, profile_id, version, submitted_at, submitted_by, payload_json
client_suggestions   profile_id, field, value, source, status (suggested|accepted|rejected), created_at
client_sources       profile_id, kind (rss|google_news|preset), value, enabled
client_competitors   profile_id, name, website, enabled
client_keywords      profile_id, term, kind (include|exclude), enabled
client_recipients    profile_id, address, status (active|unsubscribed|suppressed), added_at, unsubscribe_token_hash
personal_issues      id, profile_id, version, window_start, window_end, status, html_sha256, text_sha256, built_at, sent_at
personal_issue_items issue_id, item_id, score, reasons
personal_deliveries  issue_id, recipient, status, attempted_at, sent_at, error
audit_log            id, actor, action, target, detail_json, at
```

Recipient suppression (`recipient_health`) is shared with the general newsletter, so a bounced address is suppressed everywhere.

Retention: items older than the longest window plus a margin (for example 30 days) are deleted. Profile payloads are kept for audit; deleted clients are archived (not hard-deleted) for 12 months and then removed.

## 8. Security

- **Magic links**: 32-byte random tokens, stored as SHA-256 hashes, single-use for the first sign-in, expiring in 7 days. The GET page never consumes the token.
- **Sessions**: signed HttpOnly, SameSite=Strict cookies, 14-day lifetime, rotated on sign-in, revocable from the admin page.
- **CSRF**: every POST carries a per-session token and checks the Origin header (the existing same-origin check already does part of this).
- **Admin authentication**: the current shared ADMIN_TOKEN is not suitable once there are multiple admins and managers. Each admin signs in with their own magic link, and admin sessions are separate from manager sessions. ADMIN_TOKEN can stay as an emergency break-glass credential.
- **Authorisation**: a manager can only see and edit their own profiles. Every query takes the manager id from the session, never from the URL alone. Tests must cover cross-manager access.
- **Rate limits**: invite requests, magic-link requests, and form submissions are limited per IP and per email (for example 5 per hour).
- **Input validation**: all text fields are length-limited and HTML-escaped. URLs must be https (or http redirected to https), with a host, and must not resolve to private, loopback, or link-local addresses.
- **SSRF**: custom source URLs and client websites are fetched only through a fetch wrapper that re-checks the resolved IP on every redirect hop and blocks private ranges, metadata addresses (169.254.169.254), and non-HTTP schemes. Max response size and timeout apply.
- **Prompt injection**: website text, competitor names, and any LLM-generated suggestions are untrusted. They never become instructions. The LLM output is validated against a strict schema and length-limited, and it can only fill suggestions, never activate a profile.
- **Recipients**: the allowlist in security.yaml currently allows rankuno.com only. Team members outside rankuno.com (client staff, freelancers) need an explicit decision and a policy change (section 12).
- **Personal data**: team member emails are personal data. The invite and the unsubscribe link give a lawful way to stop sending. Under India's DPDP Act 2023, record purpose and consent. Admins can export and delete a person's data on request.
- **Logs**: never log magic-link tokens or full form payloads. Log ids.

## 9. Validation rules (summary)

| Input | Rule |
|---|---|
| Client name | Required; 2–120 chars; must not be only punctuation or only a stop word. Unique per manager (duplicate across managers is allowed, see section 10). |
| Website | Optional; https URL; resolves to a public IP; homepage must return HTML within 2 MB. |
| Recipient email | RFC-plausible; lowercase; deduplicated; domain must have MX (existing check); must be in the allowlist; not suppressed. |
| Custom RSS | https; returns a valid feed within the size limit; fetched via the SSRF wrapper; at most 10 per profile. |
| Keywords | 2–60 chars; no regex; trimmed; deduplicated. |
| Total payload | Size cap (for example 64 KB) to stop abuse. |

## 10. Edge cases

| # | Case | Handling |
|---|---|---|
| 1 | Two managers both create "GEP" | Allowed, because they may be separate teams or separate accounts. The admin page flags the duplicate. Team members who appear on both get one email per profile, or a combined email (decision, section 12). |
| 2 | Ambiguous client name ("Apple", "Oracle", "Kantar") | Auto-fill uses the website to disambiguate. Without a website, the review step shows the first 10 matching items and asks the manager to confirm or add exclude keywords before the first send. |
| 3 | Client name is a common word or acronym | Short-name rule (section 6.1): match only with an LOB term or in a title. |
| 4 | Wrong website entered | Show the homepage title to the manager on the review step. Admin can override. |
| 5 | Auto-filled competitor is wrong or a hallucination | Suggestions are never activated silently. Manager must accept each competitor. |
| 6 | Manager submits blank optional fields and never reviews | Auto-filled values are used only after a 3-day reminder. If unconfirmed by the first Monday, the feed uses only confirmed values plus LOB defaults, and the first email says so. Recommended. |
| 7 | Manager leaves the company | Admin reassigns profiles and recipients to another manager. Profiles are never orphaned: profile.manager_id must point to an active manager, enforced in code. Their magic-link sessions are revoked. |
| 8 | No news for a client in the week | Skip the send by default and log it. Optional: send "no major news this week" with the general news link. (Decision, section 12.) |
| 9 | Monday is a public holiday | Run as normal (email is not affected by the holiday); or move to the next working day. Holiday calendar in settings. (Decision, section 12.) |
| 10 | Feed run overlaps with a general issue send | They are separate jobs. The existing one-job-at-a-time runner and the 3-hour catch-up apply, so they run in sequence. |
| 11 | Team member bounces | Existing hard-failure suppression applies, shared with the general newsletter. |
| 12 | Team member unsubscribes from one client | Only that client's feed stops. A confirmation page shows which client. |
| 13 | Team member unsubscribes from all | Possible via a "all clients" link. Stored as a global suppression for personalised feeds only. The general newsletter is not affected. |
| 14 | Manager edits a profile after the Monday run started | The run uses the version snapshot taken at the start. The edit applies from the next run. |
| 15 | Two form tabs save over each other | Optimistic concurrency: each save includes the version it started from; a conflict shows a message to reload. |
| 16 | Email link scanner clicks the magic link | GET never consumes the token (section 3.1). |
| 17 | Invite forwarded to someone else | The link is bound to the first session that uses it. A forwarded link that is already used shows "link already used; ask an admin to resend". |
| 18 | Team list pasted from Outlook with names, semicolons, or angle brackets | Parse and extract emails; show the rejected lines with reasons. |
| 19 | Too many recipients for one client | Hard cap of 50 per client. Above that, admin approval. |
| 20 | Custom RSS feed is dead or slow | Counted as a source failure, like the general fetch. Auto-pause after repeated failure; manager is notified. |
| 21 | Google News queries for competitors hit rate limits | Batch competitor and keyword queries, cache 24 h, and cap the number of queries per run. If the cap is reached, lowest-priority queries are skipped and the run log says so. |
| 22 | Client profile has no active sources or keywords left after pauses | Refuse to send; flag to the manager and admin. |
| 23 | Same article matches many clients | Summary is shared (section 6.2). The email for each client includes the story once. |
| 24 | Image licence or size | Reuse the existing image pipeline (download, JPEG, cid embed). Images are cached per item, not per client, so no extra downloads. |
| 25 | Message size (images repeated across many emails) | Each email embeds its own images. Size per email must stay under the Graph limit (about 4 MB practical). Images are compressed to a max width. |
| 26 | Clock and timezone | Everything in Asia/Kolkata for windows and schedules; timestamps stored in UTC. |
| 27 | Admin changes the taxonomy after profiles exist | Profiles store the taxonomy version used. Re-run matching only on the next run; do not rewrite history. |
| 28 | Manager asks for a preview | Preview builds the feed now and sends only to the manager, marked [PREVIEW]. Rate limited. |
| 29 | Test send | Uses the existing TEST_RECIPIENTS rule: test copies go only to the test address, never to a client's team. |
| 30 | Staff member is both a team member and an admin | Works; the same address receives the feeds and can sign in separately. |

## 11. Cost and server load

### 11.1 Does this increase cost? Yes, but modestly, and the main risk is LLM cost if it's done per client per item.

What scales with what:

| Resource | Scales with | Impact | Mitigation |
|---|---|---|---|
| Daily fetch of the general sources | Sources (fixed) | No change | Already shared. |
| Custom RSS fetches | Clients × custom feeds | Small. 30 clients × 5 feeds = 150 requests/day. | Respect the existing per-host delay; cache with ETag. |
| Google News queries for competitors and keywords | Clients × (competitors + keywords) | Largest load on the fetch side. Example: 30 clients × (5 competitors + 8 keywords) ≈ 390 queries/day. Google may throttle, and the link-decoding endpoint is unofficial. | Batch terms into OR queries per client; cache 24 h; cap per run; run the weekly window only on Sunday evening. Measure before committing. |
| LLM summaries | Items (fixed per day) | No change, because summaries are per item and shared. | Already the plan (Phase 4). |
| LLM relevance per client × item | Clients × items | **The expensive option.** Example: 500 items/day × 30 clients = 15,000 calls/day. Do not do this. | Use rules-based scoring for v1. If an LLM is needed later, call it only for the top 30 candidates per client, and only once per (item, client). |
| LLM auto-fill | Clients (one-off) | Small: one call per client profile, plus manual refreshes. | Input capped; cache results per website. |
| Email sends | Recipients × clients | Example: 30 clients × 6 recipients = 180 emails/Monday. Under Microsoft 365's 30/minute limit: about 6 minutes. Daily send limits are far above this. | Existing pacing. Consider combining feeds per person (decision). |
| Image pipeline | Items (fixed) | Downloads shared; per-email embedding adds mail size, not fetch load. | Cache per item. |
| Storage | Items, profiles, deliveries | Negligible: a few MB per month. | Retention (section 7). |
| Server load | Web requests from forms | Tiny: a few dozen managers. | Single Railway process is enough. |
| SQLite | Writes from forms and runs | Fine at this scale with WAL. | Revisit if concurrent writes or the team grows past about 100 managers. |
| Hosting plan | Always-on service | Unchanged, provided Graph is used. | No SMTP on Railway Hobby/Free (already known). |

### 11.2 Rough weekly volume (example figures, to be replaced with real numbers)

Assumptions: 30 clients, 6 team recipients per client, 8 competitor and keyword queries per client, 5 custom feeds total per client.

- Monday emails: about 180.
- Google News queries per day: about 390, batched to about 30 to 60 requests with OR queries.
- LLM calls per week: about 100 at most for auto-fill and suggestions, and the existing per-item summaries.
- Storage growth: under 50 MB a year.

I have not priced LLM calls or Railway usage in this design. Once the LLM provider and token budget are chosen, I can add a cost estimate with current prices.

### 11.3 Verdict

- Operating cost: small increase, mainly from more fetches (Google News in particular) and possible LLM use. The biggest cost risk is LLM calls per client per item, which the design avoids.
- Server load: negligible. The bottleneck is the outbound email rate and external source rate limits, not compute.
- Operational load: real. Each new client adds a profile to keep reviewed, and each manager needs support. A quarterly review of the LOB defaults and competitor lists is worth budgeting.

## 12. Decisions needed from you

1. **Team members outside rankuno.com**: allowed or not? If yes, the allowlist changes and personal-data handling must be confirmed. My recommendation: rankuno.com only for v1, with an admin-approved exception list.
2. **Combined vs. separate emails** for a person on several clients' teams: one combined email per person (recommended), or one per client?
3. **Auto-fill confirmation**: require the manager to confirm before the first send (recommended), or send with auto-fill and a banner?
4. **No-news weeks**: skip (recommended), or send a short "no major news" note?
5. **Monday holiday**: send anyway (recommended), or move to the next working day?
6. **LLM for auto-fill and summaries**: which provider and budget? v1 can run rules-only.
7. **Duplicate client profiles across managers**: allow with a flag (recommended), or require admin merge?
8. **Client-facing**: should clients ever receive this? The current design is internal only, as the general newsletter is. Client delivery needs an unsubscribe-and-consent model and a separate sender policy.
9. **Admin sign-in**: move each admin to a personal magic link (recommended), keeping ADMIN_TOKEN only as break-glass?
10. **Send time**: Monday 10:00 IST proposed. Confirm.

## 13. Build plan (after the decisions above)

1. **Foundation**: new tables, magic-link invites, manager and admin sessions, CSRF, rate limits, audit log. No sending yet. Tests for cross-manager access and for the GET-does-not-consume rule.
2. **Form**: profile create/edit/draft/submit, validation, recipients, versioning. Admin list and reassignment.
3. **Auto-fill, rules-only**: website read through the SSRF wrapper, LOB defaults, suggestion review step.
4. **Matching and feed build**: client matching with the short-name rule, per-client selection, reuse of the security layer. Preview for managers.
5. **Delivery**: Monday job, per-recipient unsubscribe, list-unsubscribe header, pacing, suppression. Dry-run first with TEST_RECIPIENTS only.
6. **Client-specific sources**: custom RSS and Google News query batching with caching and caps. Measure query volume before enabling for all clients.
7. **Pilot**: GEP as the first client, with one manager and a small team, for at least two Mondays before wider rollout.
8. **Optional LLM**: auto-fill refinement and "why this matters" lines, after the rules-only pilot shows where it helps.

Each step should be buildable and testable on its own, and the general newsletter must keep working throughout.

## 14. Build status (v1, built 2026-11-02)

Decisions applied: rankuno.com recipients only; one combined email per person; manager must confirm auto-filled
values; no-news clients skipped; Monday sent as normal; rules-only auto-fill (no AI); internal only; personal
sign-in per admin and manager; Monday 10:00 IST.

**Built** (code in `rankuno_brief/personal/`, config in `config/personal.yaml`, tests in `tests/test_personal.py`):
- Invitations and sign-in: single-use magic links (hashed, 7 days), sessions (14 days, CSRF on every form, cookie HttpOnly/SameSite=Strict). GET never consumes a link.
- Admin page (`/a/`): invite managers or admins (rankuno.com only), new sign-in link, reassign clients, pause or resume clients.
- Manager pages (`/p/`): client form with validation, rules-only auto-fill (industry defaults for blank competitors and keywords), team members checked by the existing recipient security layer, activation gated on review of suggestions, preview of this week's feed on screen.
- Matching: whole-word scoring, short-name rule (no false matches on "GEP" without context), exclude keywords, per-source cap, no story repeated for the same client.
- Weekly run Mon 10:00 IST (`personal_runs` records each date once, so a restart cannot repeat it). One combined email per team member, unsubscribe per client, output gate on every email, paced sending, refused addresses don't stop the others.
- Test run (`python -m rankuno_brief personal run --test`): TEST_RECIPIENTS only, records nothing.
- Admin CLI: `personal add-admin EMAIL NAME` prints the first sign-in link (needed because the first admin cannot sign in yet).
- Switches: `PERSONAL_BASE_URL` (pages stay off without it), `PERSONAL_SEND_ENABLED` (the weekly run and invitation emails stay off without it).

**Deferred (not in v1):**
- Reading the client's website for auto-fill (step 3). The form takes the website, but nothing fetches it yet.
- Per-client source preferences and custom RSS or Google News queries (step 6). Feeds use the existing shared news only, so a client's feed is limited to what the general fetch already collects. Trade-press coverage for supply chain and procurement may be thin; check during the pilot.
- A global "stop all clients" unsubscribe. Only per-client unsubscribe exists.
- Profile version history. The form keeps a version counter only.
- Deactivating a manager from the admin page. The store function exists, but no page calls it yet; reassign their clients first.
- Pilot (step 7): GEP with one manager and a small team, for at least two weeks. Not started: it needs real people and PERSONAL_SEND_ENABLED=true, which only you can switch on.

**Note on activation:** a feed covers news since the client was activated, up to 7 days back. A client activated on Saturday gets Sat–Mon news in its first Monday feed.

## 15. Approval and admin flow (built 2026-11-02, replaces the password login)

- **Default admins**: Rajat Singh, Naeem Gari and Prithviraj Pednekar (`DEFAULT_ADMINS` in `store.py`) are created on every start and cannot be demoted from the web. They sign in by emailed link only: `personal admin-links` emails each a fresh link.
- **Invite**: an admin invites a manager by email. The manager signs in and drafts clients, but cannot activate them yet.
- **Approval request**: "Submit for approval" (one open request at a time) emails all three admins a sign-in link to the admin page.
- **Decision**: any one admin approves or rejects (rejection needs a reason). The first decision wins; the manager is emailed, and the other admins are told. Approval lets the manager activate clients; team emails still wait for the manager to confirm the suggested values.
- **Admin edits**: admins can edit any client. When a manager changes an active client, all admins are emailed.
- **Audit log**: every invite, request, decision, client change, activation, reassignment and status change is recorded and shown on the admin page.
- **Removed**: the `/login` password sign-in and the `ADMIN_TOKEN` back door. `ADMIN_TOKEN` now only switches on the old status page.
