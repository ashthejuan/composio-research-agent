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
| 2026-11-23 | — | The human set up `.env`. Live smoke: `COMPOSIO_API_KEY` (a `ck_`-style key, not a project `ak_` key) gets a 401 `APIKey_InvalidAPIKey` from `backend.composio.dev` on the `x-api-key`, `x-user-api-key` and Bearer headers | Applied the PRD §3 fallback rule. Search and scrape use the direct backend (Exa + httpx). Composio coverage comes from the public catalog at `docs.composio.dev/toolkits` (1843 slugs), with source `composio_docs_catalog`. Human to supply a valid Composio project key (usually `ak_…`) |
| 2026-11-23 | — | Gemini (`gemini-4-neon`) returns 429 RESOURCE_EXHAUSTED: quota or billing tier | Not needed until Phase 3 (L5). Human to enable paid-tier billing on the Gemini key |
| 2026-11-23 | — | Human set the extractor model to `gpt-6-luna` in `.env` | Used for pass 1 |
| 2026-11-23 | Amazon Selling Partner, GoHighLevel, Pylon, Netlify, Devin, Otter AI | Composio slug mapping needed judgement: `amazon` is a generic Amazon OAuth toolkit, not SP-API, and several apps only have `_mcp` toolkits | Reviewed each mapping against its docs page. Amazon SP-API is marked unavailable. GoHighLevel uses `highlevel_location`. The `_mcp` toolkits are kept |
| 2026-11-23 | 72 apps | Vendor GitHub orgs were missing from `official_domains`, so a vendor's own repos (for example `github.com/mondaycom/mcp`) were tiered as community | Added the orgs, each verified on its GitHub org page, before pass 1. This is an input-data fix, not prompt tuning |
| 2026-11-23 | — | Gemini verifier key env var renamed to `META_API_KEY` | Code, smoke, tests, and `.env.example` now read `META_API_KEY` (still Gemini via `google-genai`) |
| 2026-11-23 | — | Verifier switched to Meta Muse Spark (`muse-spark-1.3-contributor`) | `verify_claim` uses OpenAI SDK → `https://api.meta.ai/v1` with `META_API_KEY`; `google-genai` removed |
