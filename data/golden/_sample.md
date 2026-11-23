# Golden-set sample (FR-07)

Selected 2026-11-23. Selection used only the registry (category, kind) plus pass-1 `record_confidence`. The agent's field values were **not** looked at when choosing apps.

**Excluded on purpose**
- Apps used in Phase 1 dry runs or prompt tuning, because they are contaminated: Linear (73), Firecrawl (56), Monday.com (76).
- Apps named in the pre-freeze code review's error list, so the sample is not biased toward known errors: Gladly (20), Consensus (94), Smartsheet (79), Attio (4).

## Golden set: 20 apps, 2 per category

| # | App | Category | Kind | Pass-1 conf. | Why it is in the sample |
| - | --- | -------- | ---- | ------------ | ----------------------- |
| 2 | HubSpot | CRM and Sales | saas_api | medium | OAuth + private-app tokens, marketplace listing |
| 10 | DealCloud | CRM and Sales | enterprise_platform | medium | Enterprise / customer-only access (gated) |
| 11 | Zendesk | Support and Helpdesk | saas_api | high | Several auth methods (OAuth, API token, basic) |
| 15 | Pylon | Support and Helpdesk | saas_api | medium | Newer vendor, MCP-flavoured |
| 21 | Slack | Communications and Messaging | saas_api | high | OAuth, app directory review |
| 28 | WhatsApp Business | Communications and Messaging | saas_api | high | Meta app review / business verification (production gate) |
| 32 | Meta Ads | Marketing, Ads, Email and Social | saas_api | high | Marketing API access tiers, app review (production gate) |
| 35 | Mailchimp | Marketing, Ads, Email and Social | saas_api | medium | API key + OAuth, self-serve |
| 49 | Amazon Selling Partner | Ecommerce | saas_api | high | Developer registration / partner gate (production gate) |
| 50 | fanbasis | Ecommerce | saas_api | **low** | Low documentation (atypical) |
| 53 | Ahrefs | Data, SEO and Scraping | saas_api | high | API needs a paid plan |
| 58 | Sherlock | Data, SEO and Scraping | oss_tool | **low** | OSS CLI, no auth (atypical) |
| 61 | GitHub | Developer, Infra and Data platforms | saas_api | medium | REST + GraphQL, official MCP |
| 67 | Snowflake | Developer, Infra and Data platforms | enterprise_platform | medium | Trial / enterprise, key-pair JWT |
| 74 | Jira | Productivity and Project Management | saas_api | medium | Atlassian OAuth 2.0 (3LO) + API tokens, Marketplace |
| 75 | Asana | Productivity and Project Management | saas_api | high | PAT + OAuth, self-serve |
| 81 | Stripe | Finance and Fintech | saas_api | medium | API keys, test mode, Connect |
| 90 | PitchBook | Finance and Fintech | enterprise_platform | high | Contact sales (commercial gate) |
| 91 | NotebookLM | AI, Research and Media-native | enterprise_platform | high | Enterprise API only; consumer product has none (atypical) |
| 98 | Mermaid CLI | AI, Research and Media-native | cli | **low** | Local CLI, no auth (atypical) |

**Coverage checks**
- All 10 categories, 2 apps each.
- Confidence spread: 9 high, 8 medium, 3 low.
- Self-serve examples: Mailchimp, Asana, Stripe, GitHub, Zendesk. Gated or paid examples: DealCloud, PitchBook, Ahrefs, Snowflake, NotebookLM.
- Production-gate examples: Meta Ads, WhatsApp Business, Amazon SP-API, Slack, HubSpot.
- Atypical or low-documentation examples: fanbasis, Sherlock, Mermaid CLI, NotebookLM.
- Auth patterns covered: OAuth2, API key, basic, bearer/PAT, JWT/key-pair and none.

## Holdout: 5 apps, labelled only in Phase 4

| # | App | Category |
| - | --- | -------- |
| 7 | Zoho CRM | CRM and Sales |
| 30 | Vonage | Communications and Messaging |
| 46 | Squarespace | Ecommerce |
| 84 | Paygent Connect | Finance and Fintech |
| 100 | Grain | AI, Research and Media-native |

## How labels are produced (disclosed on the case-study page)

1. **AI-drafted, blind.** For each golden app, an independent Claude research subagent drafts the 6 labels from official docs. It never sees `data/pass1` or `data/work`, and it is a different model family from the extractor (`gpt-6-luna`). Drafts go to `data/golden/drafts/<id>.json`, each with a URL and a verbatim quote.
2. **Human-verified.** The human opens each draft with `python scripts/label.py <app>`, checks the cited docs, and accepts, edits or marks each field ambiguous. Only confirmed labels in `data/golden/<id>.json` are scored.
3. Time spent and edit counts are recorded in `data/human_log.md`.
