Implementation-ready PRD

v1.3 · 23 Nov 2026 · refined against `assignment.md`. v1.3: verifier switched to Muse Spark (`muse-spark-1.3-contributor`) via Meta Model API

# **App Intelligence Research Agent**

**Automated API, Authentication & Integration Research for Composio**

Product Requirements Document · AI Product Operations Take-home Assignment

---

# **0. Changes in v1.1**

v1.0 was solid on research mechanics. It was weaker on the parts the assignment cares about most: verification, honesty, and presenting the work in two minutes. The changes in v1.1:

| # | Change | Why (assignment says) |
| - | ------ | --------------------- |
| 1 | Added an assignment → requirement traceability matrix (§1.4) | Every graded item maps to a requirement and acceptance check |
| 2 | Composio SDK/MCP is now the agent's **tool layer**, not only a coverage lookup (§3, FR-02) | "Using Composio's own SDK and MCP to build it is in the spirit of the role" |
| 3 | Evidence is now **per claim**: each field carries its own value, URL and verbatim quote (FR-05) | Claim-level accuracy and "Evidence: the docs URL behind each answer" need it |
| 4 | Access is split into **credential access** and **production/distribution gate** (FR-03) | Many apps are free for a developer but need app review or a partnership to ship a multi-tenant OAuth app. That split is a key pattern |
| 5 | Added the "and more" fields: free tier/trial, sandbox, webhooks, official SDKs, OAuth app review, open-source/self-host, rate-limit notes (FR-03) | "…and more.." |
| 6 | Buildability score is **computed in code** from extracted fields, with anchored rubric levels. The LLM does not pick the score (FR-04) | Consistency and explainability in the interview |
| 7 | Verification is the **centre of the project**. Quote-grounding, a browser-render loop (promoted from P2), an independent verifier model and targeted re-research are all P0 (FR-06) | "Build real verification loops (agent, browser-use, and other means)… Accuracy is what matters most" |
| 8 | Accuracy protocol is now rigorous: a blind golden set, a frozen pass-1 snapshot, an automated-only pass 2, human corrections reported separately, and a holdout spot-check (FR-07, FR-08) | "Show how accuracy moved from a lower first pass to a higher one because of those loops", honestly |
| 9 | Added a **human-intervention log** as a deliverable (FR-12) | "Tell us what it does and where it needed a human" |
| 10 | Frontend is now **one static HTML page** with embedded data, patterns as the headline, a failures section, and machine-readable exports (FR-11). React + Vite removed | "Single self-explanatory HTML page… about two minutes… easy for both an agent and a human to consume" |
| 11 | Added a **runnable proof**: CLI single-app run plus a GitHub Actions `workflow_dispatch` trigger (FR-13) | "The proof (the app you built, live link or runnable trigger)" |
| 12 | SQLite replaced by one JSON file per app plus snapshots | 6–8 h budget. Fewer moving parts |
| 13 | Priorities rebalanced to the time box, with explicit cut lines (§12) | "Better to submit as early as possible" |
| 14 | Added handling rules for atypical apps such as CLIs, OSS tools and third-party wrappers (FR-01) | Sherlock, Mermaid CLI, YouTube Transcript and NotebookLM do not fit a SaaS template |

---

# **1. Product overview**

## **1.1 Problem statement**

Before Composio builds a toolkit for an app, it researches that app. The research covers:

- What the app does and which category it belongs to.
- Authentication methods and credential requirements.
- API availability, type and breadth.
- Developer access restrictions, both for getting credentials and for shipping to customers.
- Existing MCP implementations and existing Composio coverage.
- Technical and commercial integration blockers.

Doing this by hand across hundreds of apps does not scale.

## **1.2 Proposed solution**

Build a research agent. It investigates a predefined list of 100 apps, extracts integration facts with a verbatim quote and URL for each claim, and puts every claim through automated verification loops. The output is one structured, evidence-backed dataset.

Analytics run on that dataset to find cross-app patterns: which auth dominates, which categories are self-serve or gated, the most common blockers, and where the easy wins are compared with what needs outreach. Everything is presented on one self-explanatory HTML page.

## **1.3 Objectives**

1. Automate research across all 100 apps, with Composio as the agent's tool layer.
2. Produce consistent records with evidence for each claim.
3. **Measure accuracy honestly, and show it improving from pass 1 to pass 2 because of the verification loops.**
4. Find patterns and sort apps into easy wins, moderate effort and needs outreach.
5. Publish a single HTML case study that a reviewer understands in about 2 minutes without narration.
6. Ship a reproducible pipeline and a runnable trigger through GitHub.

**Primary success metric:** the share of claims that are accurate and backed by evidence, measured on a human-labelled golden set. Volume processed is not the metric.

## **1.4 Assignment traceability**

| Assignment requirement | PRD requirement | Verified by (acceptance) |
| ---------------------- | --------------- | ------------------------ |
| Category + one-line description | FR-03 | AC-2 |
| Auth method(s) | FR-03 | AC-2, golden set |
| Self-serve vs gated | FR-03 (credential access + production gate) | AC-2, golden set |
| API surface + existing MCP | FR-03 | AC-2, golden set |
| Buildability verdict + main blocker | FR-04 | AC-5 |
| Evidence URL per answer | FR-05, FR-06 | AC-3 |
| "and more" | FR-03 extended fields | AC-2 |
| Find the patterns (headline, up top) | FR-09, FR-10, FR-11 | AC-10 |
| Built with an agent; Composio SDK/MCP | §3, FR-02 | AC-7 |
| Where it needed a human | FR-12 | AC-11 |
| Verify accuracy on a sample, hits and misses | FR-07, FR-08 | AC-8, AC-9 |
| Verification loops (agent, browser-use, other) | FR-06 | AC-7 |
| Accuracy improved from first pass to final | FR-08 | AC-9 |
| Single HTML page, ~2 min, human + agent consumable | FR-11 | AC-12 |
| Proof: live link or runnable trigger | FR-13 | AC-13 |
| Say on the page where the agent was wrong or defeated | FR-11 "Where we were wrong" | AC-9, AC-12 |
| Submit live link + repo with README | FR-13 | AC-14 |

# **2. Scope**

## **2.1 In scope**

- Research of all 100 predefined apps.
- Official documentation discovery through Composio-provided search and scrape tools.
- Auth, API surface, access and gating analysis.
- MCP availability (official vendor, third-party) and existing Composio toolkit coverage.
- Buildability scoring computed in code.
- Evidence for each claim, with verbatim quotes.
- Automated verification loops and human golden-set verification.
- Cross-app analytics and opportunity buckets.
- A single static HTML case study, deployed.
- A runnable trigger (CLI + GitHub Actions).

## **2.2 Out of scope**

- Building 100 Composio toolkits.
- Creating developer accounts for every app.
- Calling authenticated endpoints for all apps.
- Accessing paid or partner-restricted APIs. A gate backed by evidence counts as a valid finding.
- Production-grade integrations, vendor outreach and continuous monitoring.

# **3. System architecture**

One research agent and one verification stage. Python handles orchestration, storage and validation. There is no multi-agent framework.

```
apps.json (100 apps, hints, official domains)
        │
        ▼
Orchestrator (asyncio, bounded concurrency, retries, on-disk cache)
        │
        ▼
Research agent ──tools via Composio SDK──►  web search (Exa via Composio, direct Exa fallback)
   │                                        page scrape (Firecrawl/Exa contents via Composio, httpx fallback)
   │                                        Composio toolkit catalog (deterministic coverage lookup)
   ▼
Extractor LLM → claim-level JSON (Pydantic)            ─►  data/pass1/   (frozen snapshot)
        │
        ▼
Verification loops (FR-06)
   L1 schema + rule checks      L2 URL liveness + official-domain check
   L3 quote grounding           L4 browser-render fallback (Playwright)
   L5 independent verifier LLM  L6 targeted re-research of failed claims
        │                                               ─►  data/pass2/   (automated-only snapshot)
        ▼
Human golden set (20 blind-labelled) + human corrections ─►  data/final/
        │
        ▼
Analytics (scores, patterns, buckets) → site/index.html (data embedded) + exports
```

## **3.1 Component responsibilities**

| Component | Responsibility |
| --------- | -------------- |
| Orchestrator | Runs jobs with concurrency limits, retries, caching and progress output |
| Composio SDK | The agent's tool layer: search, scrape and the toolkit catalog. Shows the work was done "in the spirit" of the role |
| Exa (via Composio) | Finds official docs, auth, pricing and MCP sources |
| Extractor LLM | Fills claim-level fields from the retrieved sources only |
| Pydantic | Enforces the schema and enums |
| Verification engine | Runs loops L1–L6, assigns a status to each claim, and triggers re-research |
| Verifier LLM | **Muse Spark 1.3 Contributor** (`muse-spark-1.3-contributor`, Meta Model API, `META_API_KEY`). Independent model from a different family than the extractor; judges claim against excerpt |
| Playwright | Renders docs pages that are JS-heavy or block bots, for grounding and verification |
| JSON store | One file per app for each pass, plus a run manifest |
| Analytics | Distributions, scores, buckets and patterns |
| Site generator | Python + Jinja2 → one static `index.html` with data embedded |

**Rule:** if Composio auth or tool access blocks progress for more than 30 minutes, fall back to direct Exa/httpx clients behind the same interface. Log this in the human-intervention log. The tool interface must make switching a one-line config change.

# **4. Functional requirements**

## **FR-01: Application registry**

`data/apps.json` holds all 100 apps. Each entry has:

- `id` (1–100), `name`, `category` (one of the 10 assignment categories), `website`, `hint` (verbatim from the assignment).
- `official_domains`: the allowlist for evidence ownership checks, for example `["hubspot.com", "developers.hubspot.com"]`.
- `kind`: `saas_api` | `oss_tool` | `cli` | `third_party_wrapper` | `enterprise_platform`.
- `notes`: known ambiguity.

**Atypical apps.** Treat these explicitly instead of forcing them into a SaaS template:

| App | Handling |
| --- | -------- |
| Sherlock, Mermaid CLI | `kind: oss_tool/cli`. Auth `none`. API type `cli`. Buildability is judged on whether the tool can be wrapped as an agent tool |
| YouTube Transcript | `third_party_wrapper` (transcriptapi.com). Research the hinted provider and note that it is not an official YouTube API |
| NotebookLM | Research the Enterprise API named in the hint. Note that consumer NotebookLM has no public API |
| Paygent Connect, iPayX, fanbasis, Waterfall.io, higgsfield | Low-documentation apps. Expect `unknown` values and say so. Do not infer |
| Salesforce Commerce Cloud, Magento (Adobe Commerce) | Distinguish sandbox/dev access from licensed production access |

The CLI supports `--app <name|id>`, `--ids 1-10`, `--category`, and `--all`.

## **FR-02: Documentation discovery**

The research agent calls tools **through the Composio SDK**: a web search toolkit (Exa) and a scrape toolkit (Firecrawl or Exa contents). It uses direct clients only as the documented fallback. Verify exact tool slugs and auth in Phase 0.

For each app, the agent runs focused queries for:

1. Official API documentation and reference (breadth).
2. Authentication documentation.
3. Developer access, pricing, free tier/trial and partner programs.
4. App review, marketplace or production gates.
5. Official MCP server.
6. Third-party MCP servers (GitHub, MCP registries).

Source priority is: official dev docs > official pricing/help docs > official GitHub org > reputable technical articles > community repos and forums. Sources are ranked by matching against `official_domains`.

For each source the agent stores the `url`, `title`, `fetched_at`, the page text (cached on disk), and a `source_tier`.

**Cost control:** cache search results and page fetches by URL hash. A re-run must not repeat a call already made unless `--refresh` is passed.

## **FR-03: Information extraction**

The extractor sees only the retrieved source text. It must not answer from memory. Every field is a **claim** (see FR-05).

| Field | Values |
| ----- | ------ |
| `category` | One of the 10 categories |
| `description` | One line, ≤ 20 words |
| `auth_methods` | Set of `oauth2`, `api_key`, `basic`, `bearer_token` (static or personal token), `jwt/service_account`, `session/other`, `none` |
| `credential_access` | `self_serve_free`, `self_serve_trial`, `paid_plan_required`, `admin_approval`, `partner_gated`, `contact_sales`, `unknown` |
| `production_gate` | `none`, `oauth_app_review`, `marketplace_listing`, `partner_program`, `unknown`. Answers what is needed before a multi-tenant app like Composio's can serve customers |
| `api_type` | Set of `rest`, `graphql`, `soap`, `rpc/websocket`, `sdk_only`, `cli`, `none_documented` |
| `api_breadth` | `narrow` (< ~10 resources/endpoint groups), `medium`, `broad` (> ~30 or a full product surface), `unknown` |
| `mcp_official` | `yes` (URL), `no_found`, `unknown` |
| `mcp_third_party` | `yes` (URL(s)), `no_found`, `unknown` |
| `composio_toolkit` | `available` (slug), `unavailable`, `unknown`. Comes **from the Composio catalog in code, never from the LLM** |
| `free_tier_or_sandbox` | `yes`, `no`, `unknown`, plus a note |
| `webhooks` | `yes`, `no`, `unknown` |
| `official_sdks` | List of languages, or `unknown` |
| `rate_limit_note` | Short text, or `unknown` |
| `open_source_self_host` | `yes`, `no`, `unknown` |
| `main_blocker` | Short text or `none`. Tagged `technical`, `commercial`, `access`, or `documentation` |
| `docs_url` | Primary API docs URL |

**Unknown is first-class.** If the sources do not support a value, the field must be `unknown`. An unsupported guess is counted as an error in accuracy scoring. An honest `unknown` is not.

## **FR-04: Buildability assessment (computed in code)**

The score is computed deterministically from the extracted fields, using the anchored rubric below. The LLM never assigns the score.

| Dimension | 0 | 1 | 2 |
| --------- | - | - | - |
| API availability | None documented / CLI not wrappable | Narrow or partial API | Medium/broad documented REST/GraphQL |
| Credential accessibility | Partner-gated / contact sales | Paid plan, admin approval or trial only | Self-serve free |
| Documentation quality | No public reference | Reference exists but is incomplete or unclear | Full public reference with auth guide |
| Agent-friendly operations | Read-only or trivial | Some write actions | Broad read + write; webhooks are a plus |
| Tooling readiness | No SDK/MCP | SDK or third-party MCP | Official MCP or official SDK + OpenAPI |

| Score | Verdict |
| ----- | ------- |
| 8–10 | Easy |
| 5–7 | Moderate |
| 3–4 | Difficult |
| 0–2 | Blocked |

Commercial gates (`credential_access` ∈ {partner_gated, contact_sales} or `production_gate` = partner_program) are **shown separately**. They send an app to the "needs outreach" bucket whatever its score. Each record stores a `score_breakdown`, so every verdict can be explained.

## **FR-05: Structured output (claim-level)**

Every field is stored as a claim object:

```json
{
  "app_id": 2,
  "app": "HubSpot",
  "pass": "pass1",
  "claims": {
    "auth_methods": {
      "value": ["oauth2", "bearer_token"],
      "evidence": [
        { "url": "https://developers.hubspot.com/docs/...",
          "quote": "verbatim excerpt from the page",
          "source_tier": "official_docs" }
      ],
      "extractor_confidence": "high",
      "verification": { "status": "pending", "loops": [], "notes": "" }
    }
  },
  "composio_toolkit": { "value": "available", "slug": "hubspot", "source": "composio_catalog" },
  "buildability": { "score": 0, "verdict": "unknown", "breakdown": {}, "commercial_gate": false, "bucket": "unknown" },
  "record_confidence": "low",
  "run": { "model": "...", "started_at": "...", "tool_calls": 0, "cost_usd": 0.0 }
}
```

The example contains placeholders, not research findings. Snapshots live under `data/pass1/`, `data/pass2/`, and `data/final/`. A snapshot is never overwritten once frozen.

# **5. Verification and quality control**

## **FR-06: Automated verification loops**

Each loop records its outcome on the claim's `verification.loops` array:

| Loop | Check | On failure |
| ---- | ----- | ---------- |
| L1 Schema + rules | Pydantic validity. Cross-field rules: OAuth2 needs an auth-doc quote; `partner_gated` + `easy` is flagged; `mcp_official: yes` needs a URL; and so on | Re-prompt the extractor once |
| L2 Source checks | URL returns 2xx (HEAD/GET). The domain is in `official_domains` for official-tier claims | Mark as insufficient → L6 |
| L3 Quote grounding | The quote appears in the fetched page text (normalised fuzzy match ≥ 0.9). This catches hallucinated evidence | If the fetch was empty or blocked → L4. Otherwise mark contradicted or insufficient → L6 |
| L4 Browser render | Render with Playwright (headless) for JS docs and bot walls, then rerun L3 | Mark insufficient. Log as needing a human if it is still blocked |
| L5 Independent verifier | Muse Spark (`VERIFIER_MODEL=muse-spark-1.3-contributor`) gets the claim and the source excerpt only, and returns `supported` / `partially_supported` / `contradicted` / `insufficient_evidence` with a reason | Not supported → L6 |
| L6 Targeted re-research | Focused queries for the failed field only (for example, "<app> API OAuth app review requirement"), then re-extract the field and rerun L2–L5 | At most 2 retries. Then set the value to `unknown` and keep the reason |

| Status | Meaning |
| ------ | ------- |
| Supported | Evidence directly supports the claim |
| Partially supported | Evidence supports only part of the claim |
| Contradicted | Evidence conflicts with the claim |
| Insufficient evidence | Evidence is unavailable or inconclusive |

The output of running all loops over all 100 apps is the **pass-2 snapshot**. Pass 2 is **automated only**: no human edits.

## **FR-07: Human verification (golden set)**

- **20 apps** are sampled and stratified across: all 10 categories (2 each), self-serve and gated, high and low extractor confidence, and different auth and API patterns. Include at least 3 low-documentation or atypical apps.
- **Labelled blind where feasible.** The human labels the golden set from official docs before looking at the agent's output for that app. Use `scripts/label.py`, which opens a blank form and the docs URLs. Each label records a URL and quote.
- **Evaluated fields** (6 per app, 120 claims in total): `auth_methods`, `credential_access`, `production_gate`, `api_type`, `mcp_official`, `main_blocker` (category-level match).
- **Holdout spot-check (P1):** 5 more apps, labelled after pass 2. This shows that the loop improvements carry over beyond the golden set.

## **FR-08: Accuracy measurement**

Accuracy is measured per claim:

\\[ Accuracy = \frac{Correct\ claims}{Total\ evaluated\ claims}\times100 \\]

**Scoring rules** (fixed before pass 1 is scored, and identical for every pass):

- Set fields (`auth_methods`, `api_type`): correct if the set equals the golden set on primary methods. Partial overlap counts as wrong for accuracy and is also reported as "partial".
- Enum fields: exact match.
- `unknown` where the golden value is known: counted as **"abstained"**. It is reported separately and is not counted as correct. A confident wrong value is counted as **"wrong"**. Report both, because abstaining is better than hallucinating.

**Report:**

- Pass-1 accuracy (raw agent), pass-2 accuracy (after automated loops), and final (after human corrections, labelled as such).
- Claims evaluated, number wrong, number abstained.
- Accuracy per field, and an **error taxonomy**: hallucinated evidence, outdated docs, wrong-product confusion, gate misread, MCP missed, over-inference, and so on.
- For each loop: how many errors it caught. This attributes the accuracy gain to specific loops.
- Every correction, with a before → after diff.

**Honesty rule.** Human corrections to golden-set apps do not count toward the pass-2 number. Any prompt or rule change made after inspecting golden-set errors must be disclosed. The holdout result must be reported next to it.

# **6. Analytics and insights**

## **FR-09: Cross-app analytics**

| Analysis | Output |
| -------- | ------ |
| Authentication distribution | OAuth2 vs API key vs other, including multi-method |
| Credential access | Self-serve vs paid vs admin vs partner/contact-sales |
| Production gate | Share needing app review or partnership despite free dev access |
| API maturity | REST vs GraphQL vs other |
| MCP landscape | Official vs third-party vs none, compared with Composio coverage |
| Buildability | Easy / moderate / difficult / blocked |
| Category comparison | Category × access and category × auth matrices |
| Common blockers | Ranked by count and by type |
| Opportunities | Easy wins without Composio coverage, and outreach targets |

Each pattern is written as a **plain-English headline** with its number, for example "X of 100 offer OAuth2; Y of those still need app review to go multi-tenant". The page shows the top 5 headlines first.

## **FR-10: Opportunity prioritization**

1. **Easy wins:** score ≥ 8 and no commercial gate. Highlight those with no Composio toolkit yet.
2. **Moderate effort:** score 5–7 and no commercial gate.
3. **Needs outreach / BD:** commercial gate present, whatever the score.
4. **Technically blocked:** score ≤ 4 and no commercial gate (limited or no API, or not useful for agent actions).

Every bucket assignment comes from the data and can be traced through `score_breakdown`.

# **7. Frontend requirements**

## **FR-11: HTML case study (single static page)**

One `index.html`, generated by Python + Jinja2 with the dataset embedded as JSON. It uses vanilla JS for filtering and has no backend or LLM dependency. It works offline once loaded, is responsive, and supports light and dark themes.

**Section order (headline first, designed for a 2-minute read):**

| # | Section | Contents |
| - | ------- | -------- |
| 1 | Headline patterns | The 5 top findings in one sentence each, plus key numbers (100 apps, % self-serve, % OAuth2, accuracy pass1 → pass2) |
| 2 | Opportunity matrix | 4 buckets with app chips. Easy wins without Composio coverage are called out |
| 3 | Pattern charts | Auth, access, production gate and MCP distributions, and the category × access heatmap |
| 4 | Findings explorer | Searchable, filterable 100-row table. Filters: category, auth, credential access, production gate, API type, MCP, verdict, verification status. Each row expands to show claims, quotes, evidence URLs and score breakdown |
| 5 | Verification | Accuracy pass1 → pass2 → final, per-field accuracy, errors caught per loop, golden-set hits and misses table, holdout result |
| 6 | Where we were wrong | Apps that defeated the agent, remaining known errors, and abstentions |
| 7 | How it was built | Pipeline diagram, Composio tool usage, and the human-intervention log (FR-12) |
| 8 | Proof & reproduce | Repo link, the runnable trigger, a sample run log, and the one-command run |

**Agent-consumable:** the page links `data/apps.json`, `data/apps.csv`, and `llms.txt` (a short description of the dataset, its schema and the files). The same JSON is embedded in `<script type="application/json" id="dataset">`.

## **FR-12: Human-intervention log**

`data/human_log.md` (also rendered on the page) records every point where a human had to step in: tool auth setup, blocked pages, ambiguous app identity, golden-set labelling, disputed verifier verdicts, and prompt fixes. Each entry has a timestamp, the app, the reason, and the action taken. The page summarises it in counts and themes.

## **FR-13: Proof, trigger and submission**

- CLI: `python -m agent research --app "Linear"` runs the full research and verification for one app and prints a claim table.
- GitHub Actions `research.yml` with `workflow_dispatch` (input: app name). It runs one app and uploads the record as an artifact. This is the "runnable trigger".
- The page is deployed to Vercel or GitHub Pages (static).
- The README covers setup (env keys), the one-app run, the full run, the verification run, the accuracy report, and site generation.

# **8. Non-functional requirements**

| Requirement | Specification |
| ----------- | ------------- |
| Reliability | Retries with backoff. A failed app does not stop the batch. Failures are listed in the run manifest |
| Concurrency | Configurable, default 8 apps in parallel |
| Consistency | One Pydantic schema. Enums are enforced |
| Traceability | Every claim has a URL and a verbatim quote, or is `unknown` |
| Cost control | On-disk cache. Cost and token totals are logged per run |
| Reproducibility | Full pipeline runs from the README. Snapshots are committed |
| Transparency | Unknown, low-confidence and contradicted claims are labelled on the page |
| Performance | Full 100-app pass in under 30 minutes, with progress output |

# **9. Technology stack**

| Layer | Technology |
| ----- | ---------- |
| Language | Python 3.11+ |
| Orchestration | asyncio + semaphore |
| Agent tools | Composio Python SDK (search, scrape, toolkit catalog) |
| Search fallback | Exa SDK / httpx |
| Extractor LLM | OpenAI (structured outputs), configurable |
| Verifier LLM | Muse Spark 1.3 Contributor (`muse-spark-1.3-contributor`) via OpenAI SDK at `https://api.meta.ai/v1`, key `META_API_KEY`. Model configurable via `VERIFIER_MODEL` |
| Validation | Pydantic v2 |
| Browser loop | Playwright (headless Chromium) |
| Storage | JSON per app per pass, plus a run manifest |
| Analytics | Python (pandas optional) |
| Site | Jinja2 → static HTML + vanilla JS + an inline SVG/Chart.js (pinned CDN) |
| Deployment | Vercel or GitHub Pages |
| CI trigger | GitHub Actions `workflow_dispatch` |

# **10. Execution workflow**

1. **Initialize:** build and validate `apps.json`. Smoke-test the Composio tools.
2. **Golden set:** select 20 apps and label them blind (this can run in parallel with step 3).
3. **Research pass 1:** run all 100 apps and freeze `data/pass1/`.
4. **Score pass 1:** run the accuracy report against the golden set (the baseline).
5. **Verify:** run loops L1–L6 on all 100 apps and freeze `data/pass2/`. Score pass 2.
6. **Review:** apply human corrections to make `data/final/`, run the holdout spot-check, and finish the human log.
7. **Analyze:** compute scores, buckets and patterns.
8. **Generate & deploy:** build the site, deploy it, and wire up the trigger and README.

# **11. Acceptance criteria**

| ID | Criterion |
| -- | --------- |
| AC-1 | All 100 apps have pass-1, pass-2 and final records |
| AC-2 | Every record validates against the schema, and all FR-03 fields are present (`unknown` is allowed) |
| AC-3 | Every non-unknown claim has at least 1 evidence URL and a verbatim quote |
| AC-4 | No fabricated values. Unsupported claims are `unknown` or flagged |
| AC-5 | Buildability scores, breakdowns, verdicts and buckets are computed in code for all 100 apps |
| AC-6 | Composio toolkit coverage comes from the Composio catalog for all 100 apps |
| AC-7 | The agent uses Composio SDK tools, and verification loops L1–L6 run and are logged for each claim |
| AC-8 | 20 golden-set apps are labelled with evidence. The scoring rules are fixed before pass 1 is scored |
| AC-9 | Pass-1, pass-2 and final accuracy are reported with per-field accuracy, error taxonomy, catches per loop, the holdout result, and the misses shown honestly |
| AC-10 | At least 5 data-backed pattern headlines are at the top of the page |
| AC-11 | The human-intervention log exists and is summarised on the page |
| AC-12 | The page is a single deployed static page, readable in about 2 minutes, with JSON/CSV/llms.txt exports and a "Where we were wrong" section |
| AC-13 | The CLI single-app run and the GitHub Actions trigger both work |
| AC-14 | The repo README covers setup and every pipeline step. The live link and repo link are ready for submission |

# **12. Implementation priorities and time box**

Total budget is about 7.5 h. The phase plan is in `context/phases/`.

| Priority | Module | Phase |
| -------- | ------ | ----- |
| P0 | Registry, schema, Composio tool smoke test | 0 |
| P0 | Research agent pass 1 with claim-level evidence | 1 |
| P0 | Golden set + scoring rules + baseline accuracy | 2 |
| P0 | Verification loops L1–L3, L5, L6 → pass 2 | 3 |
| P0 | Accuracy report pass1 → pass2 | 3 |
| P1 | L4 browser-render loop | 3 |
| P1 | Scoring, buckets, patterns | 4 |
| P1 | HTML case study + deploy | 5–6 |
| P1 | CLI + GitHub Actions trigger, README | 6 |
| P2 | Holdout spot-check, Chart.js polish, CSV/llms.txt | 5–6 |

**Cut lines if behind schedule, in this order:** drop the holdout (disclose it on the page) → drop extended fields from the golden set evaluation (keep extracting them) → simplify the charts to CSS bars → limit L4 to the apps that failed L3. **Never cut:** evidence for each claim, the golden set, the pass1 → pass2 measurement, or the "Where we were wrong" section.

## **Final architectural decision**

One research agent and one verification stage, both using Composio as the tool layer. Python handles orchestration, scoring, storage and site generation.

**The primary success metric is not how many apps the agent processes. It is how many accurate, evidence-backed integration decisions the system produces, and how convincingly it proves that.**
