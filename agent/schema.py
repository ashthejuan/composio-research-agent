"""Pydantic v2 schema for the app-research records (PRD FR-03..FR-08).

Conventions
-----------
* Unknown is first-class. Scalar enum / free-text claims use the string
  "unknown". Set-valued claims are ``list[Literal[...]]`` whose literal set
  includes "unknown"; the only legal unknown form is exactly ``["unknown"]``
  ("unknown" may not be mixed with other values, list may not be empty).
* Enum string mappings from the PRD (snake_case):
    "jwt/service_account" -> "jwt_service_account"
    "session/other"       -> "session_other"
    "rpc/websocket"       -> "rpc_websocket"
* Always-enforced rule (PRD AC-3): a non-unknown claim needs >= 1 evidence.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Generic, Literal, Optional, TypeVar, Union

from agent.registry import Category
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

UNKNOWN = "unknown"

# ---------------------------------------------------------------- enums
AuthMethod = Literal[
    "oauth2", "api_key", "basic", "bearer_token",
    "jwt_service_account", "session_other", "none", "unknown",
]
CredentialAccess = Literal[
    "self_serve_free", "self_serve_trial", "paid_plan_required",
    "admin_approval", "partner_gated", "contact_sales", "unknown",
]
ProductionGate = Literal[
    "none", "oauth_app_review", "marketplace_listing", "partner_program", "unknown",
]
ApiType = Literal[
    "rest", "graphql", "soap", "rpc_websocket", "sdk_only", "cli",
    "none_documented", "unknown",
]
ApiBreadth = Literal["narrow", "medium", "broad", "unknown"]
YesNoFound = Literal["yes", "no_found", "unknown"]
YesNo = Literal["yes", "no", "unknown"]
BlockerType = Literal[
    "technical", "commercial", "access", "documentation", "none", "unknown",
]
SourceTier = Literal[
    "official_docs", "official_help", "official_github", "article", "community",
]
VerificationStatus = Literal[
    "pending", "supported", "partially_supported", "contradicted",
    "insufficient_evidence",
]
Confidence = Literal["high", "medium", "low"]


def _unknown_exclusive(v: list) -> list:
    if not v:
        raise ValueError("set field must not be empty; use ['unknown']")
    if UNKNOWN in v and len(v) > 1:
        raise ValueError("'unknown' must not be mixed with other values")
    if len(set(v)) != len(v):
        raise ValueError("duplicate values in set field")
    return v


AuthMethods = Annotated[list[AuthMethod], AfterValidator(_unknown_exclusive)]
ApiTypes = Annotated[list[ApiType], AfterValidator(_unknown_exclusive)]
StrSet = Annotated[list[str], AfterValidator(_unknown_exclusive)]


# ------------------------------------------------------------- evidence
class Evidence(BaseModel):
    url: str
    quote: str = Field(min_length=1)
    source_tier: SourceTier
    fetched_at: Optional[datetime] = None

    @field_validator("url")
    @classmethod
    def _http(cls, v: str) -> str:
        if not v.lower().startswith(("http://", "https://")) or len(v) < 10:
            raise ValueError("evidence url must be http(s)")
        return v

    @field_validator("quote")
    @classmethod
    def _quote_nonblank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("quote must be non-empty")
        return v


class LoopResult(BaseModel):
    loop: Literal["L1", "L2", "L3", "L4", "L5", "L6"]
    result: str
    detail: str = ""


class Verification(BaseModel):
    status: VerificationStatus = "pending"
    loops: list[LoopResult] = Field(default_factory=list)
    notes: str = ""


class Blocker(BaseModel):
    text: str
    type: BlockerType


# ---------------------------------------------------------------- claim
T = TypeVar("T")


def _value_is_unknown(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return v == UNKNOWN
    if isinstance(v, list):
        return v == [UNKNOWN]
    if isinstance(v, Blocker):
        return v.type == UNKNOWN
    return False


class Claim(BaseModel, Generic[T]):
    value: T
    evidence: list[Evidence] = Field(default_factory=list)
    extractor_confidence: Confidence = "low"
    verification: Verification = Field(default_factory=Verification)
    note: str = ""

    def is_unknown(self) -> bool:
        return _value_is_unknown(self.value)

    @model_validator(mode="after")
    def _evidence_required(self) -> "Claim[T]":
        if not self.is_unknown() and not self.evidence:
            raise ValueError("non-unknown claim requires at least one evidence item")
        return self

    @classmethod
    def unknown(cls, value: Any = UNKNOWN) -> "Claim":
        return cls(value=value)


class Claims(BaseModel):
    category: Claim[Union[Category, Literal["unknown"]]]
    description: Claim[str]
    auth_methods: Claim[AuthMethods]
    credential_access: Claim[CredentialAccess]
    production_gate: Claim[ProductionGate]
    api_type: Claim[ApiTypes]
    api_breadth: Claim[ApiBreadth]
    mcp_official: Claim[YesNoFound]
    mcp_third_party: Claim[YesNoFound]
    free_tier_or_sandbox: Claim[YesNo]
    webhooks: Claim[YesNo]
    official_sdks: Claim[StrSet]
    rate_limit_note: Claim[str]
    open_source_self_host: Claim[YesNo]
    main_blocker: Claim[Blocker]
    docs_url: Claim[str]

    @classmethod
    def all_unknown(cls) -> "Claims":
        s = Claim[str](value=UNKNOWN)
        return cls(
            category=Claim[Union[Category, Literal["unknown"]]](value=UNKNOWN), description=s,
            auth_methods=Claim[AuthMethods](value=[UNKNOWN]),
            credential_access=Claim[CredentialAccess](value=UNKNOWN),
            production_gate=Claim[ProductionGate](value=UNKNOWN),
            api_type=Claim[ApiTypes](value=[UNKNOWN]),
            api_breadth=Claim[ApiBreadth](value=UNKNOWN),
            mcp_official=Claim[YesNoFound](value=UNKNOWN),
            mcp_third_party=Claim[YesNoFound](value=UNKNOWN),
            free_tier_or_sandbox=Claim[YesNo](value=UNKNOWN),
            webhooks=Claim[YesNo](value=UNKNOWN),
            official_sdks=Claim[StrSet](value=[UNKNOWN]),
            rate_limit_note=s,
            open_source_self_host=Claim[YesNo](value=UNKNOWN),
            main_blocker=Claim[Blocker](value=Blocker(text=UNKNOWN, type="unknown")),
            docs_url=s,
        )


# ------------------------------------------------- toolkit / buildability
class ComposioToolkit(BaseModel):
    value: Literal["available", "unavailable", "unknown"] = "unknown"
    slug: Optional[str] = None
    source: Literal["composio_catalog"] = "composio_catalog"


class DimensionScore(BaseModel):
    score: int = Field(ge=0, le=2)
    driver: str = ""


class ScoreBreakdown(BaseModel):
    api_availability: DimensionScore
    credential_accessibility: DimensionScore
    documentation_quality: DimensionScore
    agent_friendly_operations: DimensionScore
    tooling_readiness: DimensionScore

    def total(self) -> int:
        return sum(getattr(self, n).score for n in type(self).model_fields)


class Buildability(BaseModel):
    score: int = Field(default=0, ge=0, le=10)
    verdict: Literal["easy", "moderate", "difficult", "blocked", "unknown"] = "unknown"
    breakdown: Optional[ScoreBreakdown] = None
    commercial_gate: bool = False
    bucket: Literal[
        "easy_win", "moderate", "needs_outreach", "technically_blocked", "unknown"
    ] = "unknown"

    @model_validator(mode="after")
    def _sum_matches(self) -> "Buildability":
        if self.breakdown is not None and self.score != self.breakdown.total():
            raise ValueError(
                f"score {self.score} != breakdown sum {self.breakdown.total()}"
            )
        return self


# ------------------------------------------------------------ run / record
class RunInfo(BaseModel):
    model: str
    verifier_model: Optional[str] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    backend: str = ""


class AppRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    app_id: int = Field(ge=1, le=100)
    app: str
    pass_: Literal["pass1", "pass2", "final"] = Field(alias="pass")
    claims: Claims
    composio_toolkit: ComposioToolkit = Field(default_factory=ComposioToolkit)
    buildability: Buildability = Field(default_factory=Buildability)
    record_confidence: Confidence = "low"
    run: RunInfo
    errors: list[str] = Field(default_factory=list)

    @classmethod
    def unknown(cls, app_id: int, app: str, pass_: str, run: RunInfo) -> "AppRecord":
        """All-unknown record, used for apps whose run failed."""
        return cls(
            app_id=app_id, app=app, pass_=pass_, claims=Claims.all_unknown(),
            composio_toolkit=ComposioToolkit(), buildability=Buildability(),
            record_confidence="low", run=run,
        )


# ------------------------------------------------------------ golden label
GOLDEN_FIELDS = (
    "auth_methods", "credential_access", "production_gate",
    "api_type", "mcp_official", "main_blocker_type",
)


class GoldenField(BaseModel):
    status: Literal["labelled", "ambiguous"] = "labelled"
    value: Optional[str | list[str]] = None
    url: Optional[str] = None
    quote: Optional[str] = None
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> "GoldenField":
        if self.status == "labelled":
            if self.value in (None, [], "") or not self.url or not self.quote:
                raise ValueError("labelled field needs value, url and quote")
        elif not self.note:
            raise ValueError("ambiguous field needs a note")
        return self


class GoldenLabel(BaseModel):
    app_id: int = Field(ge=1, le=100)
    app: str
    labeller: str
    labelled_at: datetime
    fields: dict[str, GoldenField]

    @field_validator("fields")
    @classmethod
    def _keys(cls, v: dict[str, GoldenField]) -> dict[str, GoldenField]:
        if set(v) != set(GOLDEN_FIELDS):
            raise ValueError(f"fields must be exactly {GOLDEN_FIELDS}")
        return v


# --------------------------------------------------------------------- io
def dump_record(record: BaseModel, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(record.model_dump_json(indent=2, by_alias=True) + "\n")


def load_record(path: str | Path) -> AppRecord:
    return AppRecord.model_validate(json.loads(Path(path).read_text()))
