# Human-intervention log (FR-12)

Every point where a human had to step in. Format: `timestamp · app · reason · action`.

| When | App | Reason | Action |
| ---- | --- | ------ | ------ |
| 2026-11-23 | — | Verifier model choice | Human chose Gemini 4 Neon (`gemini-4-neon`, paid Gemini API tier) as the L5 verifier, separate from the OpenAI extractor |
| 2026-11-23 | — | API keys are not yet provisioned (`COMPOSIO_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` missing) | Smoke test blocked on live search and catalog calls. Human to add the keys to `.env` |
| 2026-11-23 | — | Composio tool slugs (`EXA_SEARCH`, `FIRECRAWL_SEARCH`, `FIRECRAWL_SCRAPE`, `EXA_GET_CONTENTS_ACTION`) were taken from the Composio docs and cannot be confirmed without a key | Recorded in `agent/config.py` as UNVERIFIED. Confirm with `python -m agent smoke` |
| 2026-11-23 | — | Composio "Instant Toolkits" (`COMPOSIO_ACCOUNT=instant_account`) may be off by default in the project settings | Human to enable it in the Composio dashboard, or connect Exa and Firecrawl accounts and set `COMPOSIO_ACCOUNT` |
| 2026-11-23 | Paygent Connect, Waterfall.io, fanbasis, iPayX, higgsfield, Pumble, Gladly, Grain, Fathom, Devin, PitchBook | Uncertain `official_domains` in the registry | Kept best guesses and flagged them for review during golden-set labelling |
| 2026-11-23 | Netlify, Amazon SP-API, LinkedIn Ads | Allowlist domains were too broad (`netlify.app`, `amazonaws.com`, `microsoft.com`) | Removed or narrowed them in review |
