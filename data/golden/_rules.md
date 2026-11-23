# Scoring rules (FR-08): frozen before pass 1 is scored

Version 1, 2026-11-23. Every pass (pass1, pass2, final, holdout) is scored with these rules, unchanged. Each accuracy report records the sha256 of this file. If the rules ever change, all passes must be rescored and the change disclosed on the page.

## Scope

- **Apps:** the 20 golden apps in `_sample.md`, using only human-confirmed labels (`data/golden/<id>.json`). Drafts are never scored.
- **Fields evaluated (6 per app, 120 claims max):** `auth_methods`, `credential_access`, `production_gate`, `api_type`, `mcp_official`, `main_blocker_type` (the `type` of `main_blocker`).
- **Not evaluated:** `category`, which comes from the registry. The extended fields (`description`, `api_breadth`, `mcp_third_party`, `free_tier_or_sandbox`, `webhooks`, `official_sdks`, `rate_limit_note`, `open_source_self_host`, `docs_url`) are spot-checked only and do not count toward the headline accuracy.

## Outcomes per claim

| Outcome | Condition |
| ------- | --------- |
| **correct** | The agent's value matches the golden value under the field rules below |
| **wrong** | The agent states a known value that does not match. This includes a set that only partly overlaps |
| **partial** | Reported in addition to wrong: a set field whose values overlap the golden set without being equal |
| **abstained** | The agent says `unknown` but the golden value is known. Counted in the denominator and **not** correct. Reported separately because abstaining is better than hallucinating |
| **excluded** | The golden field is `ambiguous`. It is left out of the denominator and listed in the report |

The golden value may itself be `unknown`, meaning the human confirmed that public docs do not establish it. In that case the agent's `unknown` is **correct**, and any concrete agent value is **wrong**.

**Accuracy = correct ÷ (evaluated − excluded) × 100.** Also reported: wrong rate, abstention rate, partial count, per-field accuracy and per-app accuracy.

## Field rules

- **`auth_methods`** is compared as a set of the methods documented for calling the API with your own credentials.
  - The headline uses **class-level matching**: `api_key` and `bearer_token` count as one class (both are static secrets), and the other values are their own classes. Correct if the class sets are equal.
  - Strict set equality is reported as a secondary metric.
  - Webhook signing secrets and SDK-internal mechanisms are not auth methods.
- **`api_type`** is compared as a set using exact equality on `rest`, `graphql`, `soap`, `rpc_websocket`, `sdk_only`, `cli`, `none_documented`. Webhooks and SSE are not separate types.
- **`credential_access`** must match exactly. Label how a single developer gets working credentials today:
  - `self_serve_free`: free account or free developer sandbox.
  - `self_serve_trial`: only a time-limited trial.
  - `paid_plan_required`: the API needs a paid plan.
  - `admin_approval`: an org admin must grant access.
  - `partner_gated`: a partner programme or application.
  - `contact_sales`: no self-serve path.
- **`production_gate`** must match exactly. It answers what a multi-tenant integrator like Composio needs before serving other customers' accounts:
  - `oauth_app_review`: app review or verification before going live.
  - `marketplace_listing`: listing or review is required to be installable.
  - `partner_program`: partnership or commercial agreement.
  - `none`: nothing beyond the developer credentials.
- **`mcp_official`** must match exactly (`yes` / `no_found`). `yes` means an MCP server run or published by the vendor itself.
- **`main_blocker_type`** must match exactly (`technical` / `commercial` / `access` / `documentation` / `none`). It is the single biggest obstacle to building an agent toolkit today.

## Integrity

- These rules are committed to git **before** `accuracy_pass1.json` exists. Check with `git log -- data/golden/_rules.md data/reports/accuracy_pass1.json`.
- A golden label is never edited after pass-1 scoring unless a factual error is found. If that happens, the edit is logged in `data/human_log.md`, and every pass is rescored and disclosed.
