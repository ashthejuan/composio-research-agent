# App Intelligence Research Agent

I built a research agent that audits **100 apps** for Composio toolkit readiness: auth methods, how you get credentials, API/MCP surface, evidence-backed claims, and a 0–10 buildability score. Results live in one static report you can skim in about two minutes.

**Case study (live):** [https://ashthejuan.github.io/composio-research-agent/](https://ashthejuan.github.io/composio-research-agent/)

Local copy: open [`site/index.html`](site/index.html) (or serve the `site/` folder). Exports: [`site/apps.json`](site/apps.json), [`site/apps.csv`](site/apps.csv), [`site/llms.txt`](site/llms.txt).

**Headline finding:** most apps look technically easy to integrate, but free developer signup often still hides a ship gate — and several easy wins are not in the Composio catalog yet.

**Accuracy (20-app hand-labelled sample, 120 claims):**
- **First-pass automated accuracy:** `52.5%` (63/120 claims).
- **Final automated verification accuracy:** `54.2%` (65/120 claims, `+1.67` percentage points; automated verification introduced `3` regressions and filled `6` unknowns incorrectly).
- **Post-review corrected-sample agreement:** `100%` agreement with human-reviewed labels on that same 120-claim sample after `55` human quality-control corrections. This is corrected-sample agreement, **not** independently measured model accuracy (independent post-correction accuracy was not measured).

**Targeted MCP audit:** A targeted 15-app audit of apps originally classified as `mcp_official=yes` (across all 10 categories; not a full 100-app re-verification) confirmed `12` official MCPs, reclassified `2` (SendGrid, YouTube Transcript) to `no_found`, and marked `1` (iPayX) `unknown` due to insufficient verifiable evidence — updating the official MCP count to **86/100** and recalculating affected buildability scores (`data/reports/mcp_audit.json`).

---

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) (recommended) or pip
- API keys (see below)

## Setup

```bash
git clone https://github.com/ashthejuan/composio-research-agent.git
cd composio-research-agent

# Install dependencies into .venv
uv sync
# or: python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"

# Browser loop (optional but used by verification when a page needs a real render)
.venv/bin/playwright install chromium

# Env
cp .env.example .env
```

Edit `.env` to configure your models and research backend.

### Research backends: Composio MCP (primary) and Exa (fallback / direct)

The pipeline supports two research backends with distinct roles:

- **Composio (`TOOL_BACKEND=composio`, default):** Primary integration path for MCP-based tool connectivity and integration access. Auto-detects key type: a consumer key (`ck_…`) routes through **Composio Connect MCP** (`https://connect.composio.dev/mcp`, running `EXA_SEARCH`, `EXA_GET_CONTENTS_ACTION`, and `FIRECRAWL_SCRAPE`); a project key (`ak_…`) routes through the **Composio Python SDK** (can also be forced via `TOOL_BACKEND=composio_sdk`).
- **Exa + httpx (`TOOL_BACKEND=direct`):** Direct web search (Exa SDK) and documentation retrieval (`httpx`), with Playwright (`L4`) for JS-rendered pages during verification.
- **Opt-in automatic fallback (`TOOL_FALLBACK=direct`):** Fallback is **off by default** (`TOOL_FALLBACK=` empty surfaces Composio errors). When `TOOL_FALLBACK=direct` is set alongside `TOOL_BACKEND=composio` and `EXA_API_KEY`, `agent/tools/fallback.py` automatically switches to the direct Exa + `httpx` backend on a missing Composio connection, upstream auth failure, or 3 consecutive primary errors, logging a warning once and recording per-backend call counts in the run manifest (`tool_backend_counts`).

> **Execution history note:** The committed 100-app dataset in `data/pass1/`, `data/pass2/`, and `data/final/` was executed using `TOOL_BACKEND=direct` (Exa + `httpx`) because the configured Composio Exa connection returned upstream 401 authentication errors during the batch run. Composio toolkit availability was checked deterministically against the public Composio catalog (`docs.composio.dev/toolkits`, 1,843 slugs).

#### Environment variables

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | Research / extraction model (`EXTRACTOR_MODEL`, default `gpt-5`) |
| `META_API_KEY` | Independent verifier (`VERIFIER_MODEL`, default `muse-spark-1.3-contributor` via `VERIFIER_BASE_URL`) |
| `COMPOSIO_API_KEY` | Primary Composio backend: `ck_…` (Connect MCP) or `ak_…` (SDK) |
| `COMPOSIO_MCP_URL` | Composio Connect MCP endpoint (default `https://connect.composio.dev/mcp`) |
| `EXA_API_KEY` | Direct web search & docs retrieval (required for `TOOL_BACKEND=direct` or `TOOL_FALLBACK=direct`) |
| `TOOL_BACKEND` | `composio` (default; auto-detects `ck_` MCP vs `ak_` SDK), `composio_sdk`, or `direct` |
| `TOOL_FALLBACK` | Leave empty for strict mode, or set to `direct` for opt-in automatic fallback to Exa + `httpx` |

#### Switching backends

Set `TOOL_BACKEND` in `.env` or pass `--backend composio|composio_sdk|direct` on the CLI:

```bash
# Primary path (Composio MCP with ck_ key, optional automatic Exa fallback if TOOL_FALLBACK=direct)
.venv/bin/python -m agent research --app "Linear" --backend composio

# Direct Exa + httpx path
.venv/bin/python -m agent research --app "Linear" --backend direct
```

Cache lives in `data/cache/` (gitignored).

---

## View the report (no API keys)

Frozen results are already in the repo. Regenerate the HTML anytime:

```bash
.venv/bin/python -m agent site
open site/index.html   # macOS; or open the file from your OS
```

Or serve statically:

```bash
.venv/bin/python -m http.server 8080 --directory site
# then visit http://localhost:8080
```

---

## Run the agent locally

Use the project venv so imports resolve:

```bash
.venv/bin/python -m agent <command> …
```

### 1. Smoke-check tools

```bash
# Registry must validate
.venv/bin/python -m agent registry check

# With TOOL_BACKEND=direct and EXA_API_KEY set:
.venv/bin/python -m agent smoke
```

If you use a Composio consumer key (`ck_…`):

```bash
.venv/bin/python -m agent composio status
.venv/bin/python -m agent composio connect exa
```

### 2. Research one app (proof command)

```bash
.venv/bin/python -m agent research --app "Linear" --backend direct
```

Other selectors:

```bash
.venv/bin/python -m agent research --ids "1-5" --backend direct
.venv/bin/python -m agent research --category "CRM" --backend direct
.venv/bin/python -m agent research --all --backend direct   # full 100-app pass (slow / costly)
```

Records land under `data/work/` until you freeze them.

### 3. Verify (six automated checks)

```bash
.venv/bin/python -m agent verify --app "Linear" --from pass1 --to pass2 --backend direct
# or all apps:
.venv/bin/python -m agent verify --all --from pass1 --to pass2 --backend direct
```

### 4. Freeze, score, finalize, analyze, site

The committed pipeline (after research + verify work dirs exist):

```bash
.venv/bin/python -m agent freeze --pass pass1
.venv/bin/python -m agent freeze --pass pass2

.venv/bin/python -m agent score --pass pass1
.venv/bin/python -m agent score --pass pass2

.venv/bin/python -m agent finalize
.venv/bin/python -m agent analyze
.venv/bin/python -m agent site
```

Useful extras:

```bash
.venv/bin/python -m agent status --pass pass1
.venv/bin/python -m agent loopstats --pass pass2
.venv/bin/python -m agent attribute --before pass1 --after pass2
```

### 5. Tests

```bash
.venv/bin/python -m pytest -q
```

---

## What’s already committed

You do **not** need to re-run the full 100-app pipeline to review the work:

| Path | Contents |
| --- | --- |
| `data/pass1/` | Frozen first-draft research records |
| `data/pass2/` | Frozen post-verification records |
| `data/final/` | Final records (golden corrections applied) |
| `data/golden/` | Hand labels + frozen scoring rules |
| `data/reports/` | Accuracy, attribution, analytics, loop stats |
| `data/human_log.md` | Where I stepped in |
| `site/` | Generated case study + exports |

---

## Golden set & accuracy framing

1. **20 stratified apps** (2 per category, 120 claims across 6 fields) were labelled from primary docs **before** scoring.
2. Frozen scoring rules in `data/golden/_rules.md` score pass 1, pass 2, and final identically.
3. **Final automated accuracy is 54.2%** (`65/120`), up `+1.67` percentage points from the `52.5%` (`63/120`) first pass — demonstrating that automated verification alone was insufficient to resolve ambiguous claims (it also introduced `3` regressions and filled `6` unknowns incorrectly).
4. Human review acted as a **quality-control mechanism**, correcting `55` incorrect or missing values on the evaluation sample to reach `100%` corrected-sample agreement on that same 120-claim sample. Independent post-correction model accuracy was not measured.

---

## Repo layout

```
agent/           # CLI + research, verify, score, analytics, site generator
data/
  apps.json      # 100-app registry
  pass1|pass2|final/
  golden/
  reports/
  human_log.md
site/            # Static case study (open index.html)
templates/       # Jinja source for the report
tests/
.env.example
```

---

## Known limitations

- Automated verification improved claim accuracy by only 1.67 percentage points (52.5% → 54.2%) and introduced 3 regressions plus 6 incorrectly filled unknowns; insufficient source evidence and misread partial documentation pages remain the primary bottlenecks.
- Human review achieved 100% agreement on the 120-claim evaluation sample after 55 corrections, which is a quality-control measure on that sample rather than independently measured post-correction model accuracy.
- The 100-app batch run executed on `TOOL_BACKEND=direct` (Exa + `httpx`) because the configured Composio Exa connection returned upstream 401 authentication errors; Composio coverage came from the public catalog (`docs.composio.dev/toolkits`).
- The targeted MCP audit covered 15 of the 89 originally-flagged `mcp_official=yes` apps (3 corrections applied); a full 100-app manual MCP re-audit was not run.
- Five holdout apps were kept out of the 20-app hand-labelled sample (they remain in the 100-app dataset and are disclosed in the report).

---

## Where I stepped in

Auth/tool setup, golden labelling and corrections, a few registry domain fixes, model/verifier config, and pipeline fixes. Full log: [`data/human_log.md`](data/human_log.md) (also summarised on the report).
