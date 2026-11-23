# App Intelligence Research Agent

I built a research agent that audits **100 apps** for Composio toolkit readiness: auth methods, how you get credentials, API/MCP surface, evidence-backed claims, and a 0–10 buildability score. Results live in one static report you can skim in about two minutes.

**Case study:** open [`site/index.html`](site/index.html) in a browser (or serve the `site/` folder). Exports: [`site/apps.json`](site/apps.json), [`site/apps.csv`](site/apps.csv), [`site/llms.txt`](site/llms.txt).

**Headline finding:** most apps look technically easy to integrate, but free developer signup often still hides a ship gate — and several easy wins are not in the Composio catalog yet.

**Accuracy (20-app hand sample):** 52.5% first draft → 54.2% after automated checks → 100% after my corrections on disagreements.

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

Edit `.env`. Minimum for a **direct** run (what this project used end-to-end):

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | Research / extraction model |
| `META_API_KEY` | Independent verifier (Meta Muse Spark) |
| `EXA_API_KEY` | Search when `TOOL_BACKEND=direct` |
| `TOOL_BACKEND` | `direct` (working path) or `composio` |

Optional Composio path:

| Variable | Purpose |
| --- | --- |
| `COMPOSIO_API_KEY` | `ck_…` (Connect MCP) or `ak_…` (SDK) |
| `TOOL_FALLBACK` | Set to `direct` to fall back if Composio auth fails |

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

## Golden set (how I measured accuracy)

1. I labelled **20 apps** (2 per category) from primary docs **before** scoring.
2. Scoring rules in `data/golden/` were frozen; the same rules score pass 1, pass 2, and final.
3. Pass 2 is automated only. Final accuracy includes my corrections and is reported separately so the automated lift stays honest.

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

- The provided Composio consumer key could not authenticate search/scrape MCP calls; I ran with `TOOL_BACKEND=direct` (Exa + httpx) and looked up Composio coverage from the public catalog.
- Automated checks improved the sample slightly but also introduced a few regressions on multi-method auth — those are listed on the report under “Where I was wrong.”
- Five apps were left out of the hand-checked sample (still in the full dataset); the report discloses them.

---

## Where I stepped in

Auth/tool setup, golden labelling and corrections, a few registry domain fixes, model/verifier config, and pipeline fixes. Full log: [`data/human_log.md`](data/human_log.md) (also summarised on the report).
