# Pass 1 → Pass 2 Change Disclosure (FR-08 / Phase 3)

Every input, rule, prompt, or verification-pipeline change made between frozen `data/pass1/` and frozen `data/pass2/` is disclosed below. No golden-set labels or scoring rules were modified (`rules_sha256` is identical: `8d32f4df9c1988c6bef1029cd7ca24f4e724d102ac1c59edbf3297cf633ca4d3`). No manual edits were made to any record in `data/pass2/`.

## 1. Input registry edit (disclosed golden-app change)

- **Commit `be7d084`**: Added `commas.com` and `commasdocs.com` to `official_domains` for **fanbasis** (app #37, a golden app). During golden labelling it was confirmed that FanBasis, Inc. rebranded to Commas (`fanbasis.com` redirects to `commas.com`, and official API docs moved to `commasdocs.com`). This is an input-data correction, not prompt tuning.

## 2. Automated verification pipeline added for Pass 2 (L1–L6)

Pass 1 was a single-pass research extraction (`gpt-6-luna` via direct Exa + httpx). Pass 2 runs six automated verification loops over every pass-1 record:

- **L1 (schema & cross-field rules)** (`agent/loops.py`): Re-validates every claim against Pydantic models and checks 6 cross-field rules (`oauth2_quote`, `access_vs_blocker`, `mcp_yes_has_url`, `gate_official_evidence`, `graphql_quote`, `description_when_docs`). Any L1 failure prevents `supported` status and routes evaluated fields to L6.
- **L2 (source liveness & official-domain check)** (`agent/loops.py`): Probes evidence URLs (HEAD with streaming GET fallback, disk-cached) and verifies `official_domains` membership for claims tagged with an official tier.
- **L3 (quote grounding)** (`agent/loops.py`, `agent/evidence.py`): Normalises whitespace/case/smart-quotes/markdown and checks exact substring or `rapidfuzz.partial_ratio >= 90` against cached page text (`httpx.scrape`, `composio_mcp.scrape`, or `exa.snippet`).
- **L4 (Playwright browser render fallback)** (`agent/tools/browser.py`): Shared headless Chromium renderer triggered only when L3 has empty page text or an ungrounded snippet-only source. On the 100-app run, every evidence URL already had cached page text or snippet text that grounded or resolved without triggering L4 (`claims_checked = 0`).
- **L5 (independent verifier)** (`agent/verify.py`, `agent/prompts.py`): **Meta Muse Spark** (`muse-spark-1.3-contributor`, separate model family from extractor `gpt-6-luna`) judges each non-unknown, non-registry claim strictly against its field definition and the ±1200-char excerpt around its cited quote(s), with no app context beyond the product name.
- **L6 (targeted re-research)** (`agent/verify.py`, `agent/prompts.py`): For any of the 6 evaluated fields whose pre-L6 status is not `supported` (including `unknown`), runs up to 2 attempts of field-specific Exa queries, single-field re-extraction (`build_field_prompt`), and full L2–L5 re-verification. Candidates are accepted only if grounded by L3/L4 **and** rated `supported` by L5. Contradicted claims whose re-research fails are withdrawn to `unknown`; `main_blocker` runs last using the other verified fields as context.

## 3. Prompt / rule changes after inspecting Pass 1 misses

- **None to Pass 1 prompts or golden scoring rules.** The verification prompts (`build_verifier_prompt`, `build_field_prompt`) and generic per-field L6 query templates (`L6_QUERIES`) were written from the PRD FR-03/FR-06 field definitions without app-specific hardcoding.
- **Observed Pass 2 behaviour & honest regression note:**
  - Overall golden accuracy rose from **52.5% (63/120)** in Pass 1 to **54.17% (65/120)** in Pass 2 (**+1.67 pp**).
  - **5 claims fixed** (all via L6 filling pass-1 `unknown` values): HubSpot, Jira, Stripe (`credential_access`: `unknown` → `self_serve_free`), Sherlock (`auth_methods`: `unknown` → `['none']`), Mermaid CLI (`mcp_official`: `unknown` → `no_found`).
  - **3 regressions** (all on `auth_methods`, triggered by L5 marking pass-1 `partially_supported` and L6 replacing the whole multi-value set from a narrower re-research page): DealCloud (`['oauth2', 'api_key', 'bearer_token']` → `['oauth2']`), Snowflake (added `session_other`), PitchBook (`['api_key']` → `['oauth2']`).
  - **6 `abstained_to_wrong` transitions**: L6 filled 6 previously `unknown` claims with grounded values that disagreed with the golden label's classification (Pylon, Mailchimp ×2, Amazon SP-API, Snowflake, Jira).
  - No second Pass 2 iteration was run; Pass 2 is frozen as-is (`data/pass2/`) and reported transparently.
