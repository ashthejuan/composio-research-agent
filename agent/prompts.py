"""Extractor prompt + OpenAI-strict structured-output model (PRD FR-03 / FR-05).

The extractor sees ONLY numbered source excerpts. Each non-unknown field must cite
source ids and verbatim quotes. `category` (from the registry) and `composio_toolkit`
(from the catalog, AC-6) are deliberately NOT part of the extraction.

`Extraction` is designed for `client.responses.parse(text_format=Extraction)` strict mode:
every field is required, no defaults, no dict/Any, only Literal enums and nested models.
Citations come *before* the value in each field model so the model grounds first.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from agent.schema import ApiBreadth, ApiType, AuthMethod, BlockerType, Confidence, CredentialAccess, \
    ProductionGate, YesNo, YesNoFound


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str  # e.g. "S3"
    quote: str  # exact contiguous substring of that source's text, <= 200 chars


class _Cited(BaseModel):
    model_config = ConfigDict(extra="forbid")
    citations: list[Citation]
    confidence: Confidence
    note: str


class StrField(_Cited):
    value: str


class AuthMethodsField(_Cited):
    value: list[AuthMethod]


class CredentialAccessField(_Cited):
    value: CredentialAccess


class ProductionGateField(_Cited):
    value: ProductionGate


class ApiTypeField(_Cited):
    value: list[ApiType]


class ApiBreadthField(_Cited):
    value: ApiBreadth


class YesNoFoundField(_Cited):
    value: YesNoFound


class YesNoField(_Cited):
    value: YesNo


class StrListField(_Cited):
    value: list[str]


class BlockerField(_Cited):
    text: str
    type: BlockerType


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: StrField
    auth_methods: AuthMethodsField
    credential_access: CredentialAccessField
    production_gate: ProductionGateField
    api_type: ApiTypeField
    api_breadth: ApiBreadthField
    mcp_official: YesNoFoundField
    mcp_third_party: YesNoFoundField
    free_tier_or_sandbox: YesNoField
    webhooks: YesNoField
    official_sdks: StrListField
    rate_limit_note: StrField
    open_source_self_host: YesNoField
    main_blocker: BlockerField
    docs_url: StrField


EXTRACTION_FIELDS: tuple[str, ...] = tuple(Extraction.model_fields)


SYSTEM_PROMPT = """\
You are a meticulous API-integration researcher. You fill a structured record describing how \
feasible it is for a multi-tenant integration platform (like Composio) to build an agent toolkit \
for ONE application.

# Hard rules
1. Use ONLY the numbered sources provided in the user message ([S1], [S2], ...). Never answer from \
memory or general knowledge, even if you are confident. If the sources do not support a value, \
answer "unknown".
2. Every field that is not "unknown" MUST have at least one citation: `source_id` (e.g. "S2") and a \
`quote` copied VERBATIM, character for character, from that source's text. A quote is an exact \
contiguous substring of ONE source: copy-paste it, do not retype it. Keep quotes short: the shortest \
span that supports the value (a phrase or one sentence, at most 200 characters; cut at a word boundary \
rather than summarise). Never paraphrase, reorder, abbreviate, translate, or join text across "[...]" \
gaps or line breaks. Keep the source's exact spelling, casing, punctuation and symbols (including \
markdown characters such as `*`, `#`, `|`). If you cannot find an exact supporting substring, the value \
is "unknown".
3. The quote must itself support the value. A quote that merely mentions the topic is not enough.
4. Prefer "unknown" over guessing. An honest "unknown" is correct; an unsupported guess is an error.
   For unknown fields: value "unknown" (sets: ["unknown"]), citations [], confidence "low".
5. Never mix "unknown" with other values in a set field.
6. Sources are tiered. Prefer official_docs > official_help > official_github > article > community. \
Cite official sources whenever they support the value; use third-party (article/community) sources \
mainly for mcp_third_party. They must not override official documentation for any field.
7. confidence: "high" = an official source states it directly; "medium" = stated but indirect, partial \
or only in a non-official source; "low" = weak support. `note` = one short sentence of nuance or "".

# Field definitions (enums are exact strings)
- description: one line, at most 20 words, what the product does (cite a source describing it).
- auth_methods (set): how an API client authenticates.
  oauth2 = OAuth 2.0 authorization flows (authorization code, client credentials, ...).
  api_key = a static API key / secret key sent in a header or query.
  basic = HTTP Basic auth (username/password or key as username).
  bearer_token = a static or personal access token sent as a Bearer token (not obtained via an OAuth flow).
  jwt_service_account = signed JWT assertions or service-account credentials.
  session_other = cookies/sessions, HMAC request signing, or another scheme.
  none = the API/tool needs no authentication (e.g. a local CLI or open-source library).
- credential_access: can a developer get working API credentials for their OWN account/workspace? \
(Being the admin of a workspace you created yourself is still self-serve; admin_approval means someone \
else must grant access.)
  self_serve_free = anyone can sign up and get credentials for free (free plan or free developer account).
  self_serve_trial = self-serve, but only through a time-limited free trial.
  paid_plan_required = API access requires a paid plan/subscription (self-serve purchase).
  admin_approval = credentials must be granted/approved by an account or workspace admin or the vendor \
after an application (not a commercial deal).
  partner_gated = API access is only for approved partners / partner-program members.
  contact_sales = API access requires talking to sales / an enterprise contract.
- production_gate: what is needed BEFORE a multi-tenant app (one app used by many customers' accounts) \
can serve customers. This is distinct from credential_access: a developer may get a free key \
(credential_access=self_serve_free) yet still need app review to go live (production_gate=oauth_app_review).
  none = no review/listing/partnership is required to let other customers connect.
  oauth_app_review = the vendor must review/verify/approve the OAuth app (or its scopes) before public \
or unrestricted use.
  marketplace_listing = the app must be listed/approved in the vendor's marketplace or app store to be \
installed by other customers.
  partner_program = a partner program membership/agreement is required to distribute the integration.
- api_type (set): rest, graphql, soap, rpc_websocket (RPC, gRPC, JSON-RPC or WebSocket/streaming APIs), \
sdk_only (only client SDKs/libraries, no documented wire API), cli (a command-line tool), \
none_documented (sources indicate there is no public API).
- api_breadth: narrow = fewer than ~10 resources/endpoint groups; medium = about 10-30; \
broad = more than ~30 resources or a full product surface. Cite a quote that shows the scope \
(e.g. a list of resources or a statement of coverage).
- mcp_official: "yes" ONLY if the vendor itself publishes/operates an MCP (Model Context Protocol) \
server (on an official domain or the vendor's official GitHub org). "no_found" if the sources were \
searched and show no vendor MCP server, but only when you can cite a source (e.g. the vendor's docs or \
an MCP listing that is clearly community-made); otherwise "unknown". A community server is NOT official.
- mcp_third_party: "yes" if a community or other-vendor MCP server for this app exists in the sources \
(cite it); else "unknown" (use "no_found" only with a citing source).
- free_tier_or_sandbox: "yes" if a free plan, free developer account, sandbox or test environment exists; \
"no" if sources state there is none; note the specifics.
- webhooks: "yes" if the API offers webhooks/event subscriptions; "no" only if stated.
- official_sdks (set of language names, e.g. ["python", "node", "java"]): vendor-maintained SDKs only, \
lowercase language names. ["unknown"] if not shown.
- rate_limit_note: short text summarising concrete documented rate limits (e.g. "100 requests/10s \
per app"). If the sources give no concrete limit, "unknown" (never text like "not specified").
- open_source_self_host: "yes" if the product is open source or can be self-hosted; "no" if stated \
proprietary/cloud-only; else "unknown".
- main_blocker: the single biggest obstacle to building a toolkit. `text` = a short phrase; \
`type` one of: technical (no usable API, complex protocol), commercial (paid/enterprise contract, \
partner deal), access (approval, app review, admin consent, region limits), documentation (docs \
missing/unclear). Use text "none" and type "none" only if the sources show self-serve credentials, \
a documented API and no production gate (cite them). Otherwise text "unknown" and type "unknown".
- docs_url: the URL of the primary official API documentation / reference, taken from a source's URL \
or text (cite the source). "unknown" if none of the sources is the API docs.

The Composio toolkit field and the category are NOT extracted by you.
"""


def build_user_prompt(app_block: str, sources_block: str) -> str:
    return (
        f"# Application\n{app_block}\n\n"
        "# Sources (the ONLY permitted evidence; quote verbatim from the text under each header)\n"
        f"{sources_block}\n\n"
        "Fill every field of the record following the rules. Cite source ids exactly as written (e.g. S1)."
    )


# ============================================================ Phase 3: verifier (L5) + re-research (L6)
# Concise FR-03 field definitions shown to the independent verifier and appended to L6 single-field
# extraction prompts. Generic (no app-specific knowledge); derived only from PRD FR-03.
VERIFIER_FIELD_DEFS: dict[str, str] = {
    "description": "One line (<= 20 words) saying what the product does.",
    "auth_methods": (
        "Set of ways an API client authenticates: oauth2 (OAuth 2.0 flows), api_key (static key/secret in a "
        "header or query), basic (HTTP Basic), bearer_token (static or personal access token sent as Bearer, "
        "not from an OAuth flow), jwt_service_account (signed JWT / service-account credentials), "
        "session_other (cookies, HMAC signing, other), none (no auth needed). The set should list the "
        "documented methods, no more and no fewer."),
    "credential_access": (
        "Can a developer get working API credentials for their OWN account? self_serve_free (free sign-up or "
        "free plan gives API access), self_serve_trial (only via a time-limited trial), paid_plan_required "
        "(API needs a paid plan bought self-serve), admin_approval (someone else, e.g. the vendor or a "
        "workspace admin, must approve/grant access after an application), partner_gated (only approved "
        "partners), contact_sales (needs sales / an enterprise contract)."),
    "production_gate": (
        "What is needed BEFORE one multi-tenant app (used by many customers' accounts, like Composio's) can "
        "serve customers; distinct from getting a developer key. none (no review, listing or partnership "
        "needed), oauth_app_review (vendor must review/verify the OAuth app or its scopes before public use), "
        "marketplace_listing (app must be listed/approved in the vendor's marketplace to be installed by "
        "other customers), partner_program (a partner agreement is required to distribute)."),
    "api_type": (
        "Set of API styles the vendor documents: rest, graphql, soap, rpc_websocket (RPC/gRPC/JSON-RPC/"
        "WebSocket/streaming), sdk_only (only client libraries, no documented wire API), cli (command-line "
        "tool), none_documented (no public API)."),
    "api_breadth": "narrow (< ~10 resources/endpoint groups), medium (~10-30), broad (> ~30 or a full product surface).",
    "mcp_official": (
        "yes = the vendor itself publishes/operates an MCP (Model Context Protocol) server; no_found = "
        "sources show no vendor MCP server. A community-built server is NOT official."),
    "mcp_third_party": "yes = a community or other-vendor MCP server for this app exists; no_found = none found.",
    "free_tier_or_sandbox": "yes = a free plan, free developer account, sandbox or test environment exists; no = stated none.",
    "webhooks": "yes = the API offers webhooks/event subscriptions; no = stated not offered.",
    "official_sdks": "Languages of vendor-maintained SDKs (not community libraries).",
    "rate_limit_note": "Short summary of concrete documented API rate limits.",
    "open_source_self_host": "yes = open source or self-hostable; no = stated proprietary/cloud-only.",
    "main_blocker": (
        "The single biggest obstacle to building a multi-tenant agent toolkit, as {text, type}; type is "
        "technical (no usable API, complex protocol), commercial (paid/enterprise contract, partner deal), "
        "access (approval, app review, admin consent, region limits), documentation (docs missing/unclear), "
        "or none (self-serve credentials, documented API and no production gate)."),
    "docs_url": "URL of the primary official API documentation/reference.",
}

VERIFIER_INSTRUCTIONS = """\
You are an independent fact checker. Judge each claim about the product "{app}" ONLY against the \
excerpts given under that claim. Do not use outside knowledge. Excerpts are verbatim page text; the \
cited quote is marked >>> <<< when it was found.
Status (exactly one):
- supported: the excerpts directly support the FULL claimed value.
- partially_supported: the excerpts support only part of it (e.g. some listed methods, but the set \
looks incomplete or includes items not shown).
- contradicted: the excerpts conflict with the claimed value.
- insufficient_evidence: the excerpts do not establish the value (off-topic, too vague, quote missing).
Return one verdict per claim, using the claim's field name exactly. reason: <= 200 characters, name \
the decisive phrase.
"""


def build_verifier_prompt(app: str, claim_blocks: list[str]) -> str:
    return VERIFIER_INSTRUCTIONS.format(app=app) + "\n" + "\n\n".join(claim_blocks)


def build_field_prompt(app_block: str, field: str, sources_block: str, context: str = "") -> str:
    """L6 single-field re-extraction: same rules as pass 1 (system prompt), one field, FR-03 definition."""
    ctx = f"# Already-verified facts about this app (context; quote sources, not this list)\n{context}\n\n" \
        if context else ""
    return (
        f"# Application\n{app_block}\n\n{ctx}"
        "# Sources (the ONLY permitted evidence; quote verbatim from the text under each header)\n"
        f"{sources_block}\n\n"
        f"Fill ONLY the field `{field}`. Definition (PRD FR-03): {VERIFIER_FIELD_DEFS[field]}\n"
        "Follow every hard rule: each non-unknown value needs a VERBATIM quote from one source that itself "
        "supports the value; otherwise answer unknown. Cite source ids exactly as written (e.g. S1)."
    )
