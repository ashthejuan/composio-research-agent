"""Application registry: the 100-app input list (FR-01)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

Category = Literal[
    "CRM and Sales",
    "Support and Helpdesk",
    "Communications and Messaging",
    "Marketing, Ads, Email and Social",
    "Ecommerce",
    "Data, SEO and Scraping",
    "Developer, Infra and Data platforms",
    "Productivity and Project Management",
    "Finance and Fintech",
    "AI, Research and Media-native",
]
Kind = Literal["saas_api", "oss_tool", "cli", "third_party_wrapper", "enterprise_platform"]

CATEGORIES: tuple[str, ...] = (
    "CRM and Sales",
    "Support and Helpdesk",
    "Communications and Messaging",
    "Marketing, Ads, Email and Social",
    "Ecommerce",
    "Data, SEO and Scraping",
    "Developer, Infra and Data platforms",
    "Productivity and Project Management",
    "Finance and Fintech",
    "AI, Research and Media-native",
)


class App(BaseModel):
    id: int = Field(ge=1, le=100)
    name: str
    category: Category
    website: str
    hint: str
    official_domains: list[str] = Field(min_length=1)
    kind: Kind = "saas_api"
    notes: str = ""


def _default_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "apps.json"


def load_apps(path: str | Path | None = None) -> list[App]:
    p = Path(path) if path else _default_path()
    with open(p, encoding="utf-8") as f:
        return [App.model_validate(x) for x in json.load(f)]


def get_app(key: str | int) -> App:
    apps = load_apps()
    k = str(key).strip()
    if k.isdigit():
        for a in apps:
            if a.id == int(k):
                return a
    else:
        for a in apps:
            if a.name.lower() == k.lower():
                return a
    raise KeyError(f"Unknown app: {key!r}")


def parse_ids(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return out


def select(ids: str | None = None, category: str | None = None) -> list[App]:
    apps = load_apps()
    if ids:
        wanted = set(parse_ids(ids))
        apps = [a for a in apps if a.id in wanted]
    if category:
        c = category.strip().lower()
        apps = [a for a in apps if a.category.lower() == c or c in a.category.lower()]
    return apps


_CODE_HOSTS = frozenset({"github.com", "gitlab.com"})


def is_official_url(app: App, url: str) -> bool:
    """True if url's host is (a subdomain of) an official domain.

    Entries containing "/" (e.g. "github.com/sherlock-project") match host + path prefix
    (case-insensitive: GitHub org names are). A bare code-host entry such as "github.com" (the GitHub
    app itself) covers the site's own pages (/pricing, /features) but not arbitrary owner/repo paths:
    otherwise every community repo would count as official for that app.
    """
    parsed = urlparse(url if "//" in url else "//" + url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    segments = [s for s in path.split("/") if s]
    for dom in app.official_domains:
        dom = dom.lower().strip().rstrip("/")
        if dom in _CODE_HOSTS and host == dom and len(segments) >= 2:
            continue  # owner/repo on a code host: only an org-path entry can make it official
        if "/" in dom:
            path = path.lower()
            dhost, dpath = dom.split("/", 1)
            dpath = "/" + dpath
            if (host == dhost or host.endswith("." + dhost)) and (
                path == dpath or path.startswith(dpath + "/")
            ):
                return True
        elif host == dom or host.endswith("." + dom):
            return True
    return False


def check_registry(path: str | Path | None = None) -> int:
    problems: list[str] = []
    try:
        apps = load_apps(path)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: could not load registry: {e}")
        return 1
    if len(apps) != 100:
        problems.append(f"expected 100 apps, found {len(apps)}")
    if sorted(a.id for a in apps) != list(range(1, 101)):
        problems.append("ids are not unique 1..100")
    names = [a.name.lower() for a in apps]
    if len(set(names)) != len(names):
        problems.append("duplicate names")
    for c in CATEGORIES:
        n = sum(1 for a in apps if a.category == c)
        if n != 10:
            problems.append(f"category {c!r} has {n} apps, expected 10")
    for a in apps:
        if not a.official_domains:
            problems.append(f"app {a.id} {a.name} has no official_domains")
    if problems:
        print("FAIL:")
        for p in problems:
            print(f"- {p}")
        return 1
    print("OK: 100 apps, 10 categories x 10")
    return 0
