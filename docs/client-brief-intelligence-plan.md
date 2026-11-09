# Client Brief: intelligence upgrades (plan)

Status: planned 2026-11-09. Phase 1 and Phase 2 are being built now; the decisions in section 5 are not made yet.

## 1. The five requests, and what each one needs

| # | Request | What it needs | Status |
|---|---|---|---|
| a | Key takeaways of each article | The full article text. The feed snippet is often blank or one line. | Phase 1: article reader + extractive takeaways (no AI). |
| b | How and why it needs attention for GEP | A suggested action per story, from rules on what matched and where. | Phase 1: rule-based actions. An AI rewrite is a later option (decision 5.1). |
| c | Where competitors are mentioned on the web | Competitor names in article text, not only in headlines. | Phase 1: competitor watch from the article text of stories we already fetch. |
| d | Where GEP is mentioned without a link | Client names in article text, and whether the article links to GEP's site. | Phase 1: unlinked-mention check on the same articles. |
| e | Magazines, news, forums, discussion groups, social media | More sources. Magazines and forums can be added free. Social media needs a paid API. | Phase 2: Inbound Logistics (magazine) and r/procurement (forum). Social media: decision 5.3. |

## 2. How it works (Phase 1)

1. **Article reader** (`personal/articles.py`). For stories in the client feeds and for the wider news pool that touches a client's topics, fetch the article page once, extract the main text and the links it contains, and cache both in the database. Each host is visited with the same politeness delay the general fetch uses. Failed pages are retried after 7 days.
2. **Takeaways** (`personal/analysis.py`). Split the article text into sentences, score each by how many client, competitor and keyword terms it contains (headline terms weigh more), and keep the best three in their original order. No AI is used, so the takeaways are the article's own sentences.
3. **Suggested action** (rules). One or two lines per story, such as "Competitor activity: check it against GEP's positioning on Coupa", "GEP is named in the article without a link to gep.com", or "Core topic: consider a sales or content angle".
4. **Mentions and competitor watch**. Scan the cached article text for the client's name and aliases, and for competitor names. Each hit is recorded with its publisher, its headline, and a snippet of the sentence. A mention counts as "cited" if the article links to the client's website, and "unlinked" if it doesn't.
5. **Email**. Each story card shows its takeaways and suggested action. Each client section gains two blocks: "Where GEP is mentioned" (linked and unlinked) and "Competitor watch".

## 3. Cost and load

- Article reads: at most 150 per daily run, cached for 30 days. Roughly 60 to 150 new pages per day across all clients, each a single request, spread out by the politeness delay. Expect a few minutes of extra work per run.
- No new paid services in Phase 1 or Phase 2.
- Email size: each story adds 2 to 3 lines of text. Pictures stay as they are now.

## 4. Limits to tell the team

- Mentions are found in the articles we fetch from our sources, not across the whole web. Anything outside those sources is not seen until a search or social API is added (decision 5.2).
- Sentence-based takeaways are the article's own words. They can read awkwardly. An AI summary would read better.
- Social media is not covered at all in Phase 2.

## 5. Decisions needed

1. **AI for takeaways and suggested actions.** Needs a provider and a budget. Without it, the takeaways are extracted sentences and the actions are rules.
2. **Web-wide search for competitor and brand mentions.** Needs a search API (e.g. Bing or Brave) with a monthly budget. Without it, only our sources are scanned.
3. **Social media.** X needs a paid API (about $50 to $80 a month, not yet approved). LinkedIn has no open API. Without a decision, social media stays out of the brief.
4. **Reddit rate limits.** Several subreddits returned "too many requests" when tested. Each one may need its own run slot or a gap between requests.

## 6. Build order

- Phase 1: article reader, takeaways, suggested actions, mention and competitor scan, email blocks, tests.
- Phase 2: Inbound Logistics and r/procurement as personal-only sources. Test that the general brief does not change.
- Phase 3 (after decisions 5.1 to 5.3): AI takeaways, web-wide search, social media.
