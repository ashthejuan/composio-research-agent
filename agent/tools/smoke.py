"""Phase 0 smoke test: `python -m agent smoke`. Never prints secret values."""
from __future__ import annotations

import asyncio
import os
import re

from pydantic import BaseModel

from agent.cache import get_cache
from agent.config import COMPOSIO_TOOL_SLUGS, get_settings

_KEYLIKE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|AIza[A-Za-z0-9_\-]{10,}|[A-Za-z0-9_\-]{32,})")


_SECRET_ENV = ("COMPOSIO_API_KEY", "OPENAI_API_KEY", "META_API_KEY", "EXA_API_KEY")


def scrub(msg: object) -> str:
    """Redact the actual configured secret values (any format, e.g. short Composio `ak_` keys),
    then anything key-shaped, then truncate."""
    text = str(msg)
    for name in _SECRET_ENV:
        val = os.environ.get(name)
        if val and len(val) >= 6:
            text = text.replace(val, "[REDACTED]")
    return _KEYLIKE.sub("[REDACTED]", text)[:300]


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {scrub(e)}"


class _Ping(BaseModel):
    ok: bool


async def _run() -> int:
    from agent.tools.base import get_tools

    s = get_settings()
    keys = {"COMPOSIO_API_KEY": s.composio_api_key, "OPENAI_API_KEY": s.openai_api_key,
            "META_API_KEY": s.meta_api_key, "EXA_API_KEY": s.exa_api_key}
    print("Keys:", ", ".join(f"{k}={'present' if v else 'missing'}" for k, v in keys.items()))
    from agent.tools.base import resolve_backend

    resolved = resolve_backend()
    print(f"Backend: {s.tool_backend} -> {resolved} | slugs: {COMPOSIO_TOOL_SLUGS}"
          f" | TOOL_FALLBACK={os.environ.get('TOOL_FALLBACK') or '(none)'}")

    search, scrape, catalog = get_tools()
    ok = {"search": False, "scrape": False, "catalog": False}
    used = resolved
    query = "Linear API authentication"
    results = []

    async def do_search(tool):
        return await tool.search(query, n=5)

    def served(tool) -> str:
        """Which backend served the last call (fallback wrappers count per backend)."""
        c = getattr(tool, "counts", None)
        return f"{resolved}|counts={c.snapshot()}" if c is not None else resolved

    try:
        if resolved.startswith("composio") and not s.composio_api_key:
            print("SKIPPED: COMPOSIO_API_KEY missing (search/scrape via composio)")
            raise LookupError
        results = await do_search(search)
        ok["search"] = bool(results)
        print(f"search[{served(search)}]: {len(results)} results; top: {results[0].url if results else '-'}; "
              f"snippet chars={[len(r.snippet) for r in results]}")
    except LookupError:
        pass
    except Exception as e:
        print(f"search[{resolved}] FAILED: {_err(e)}")
        if type(e).__name__ in ("ComposioConnectionMissing", "ComposioUpstreamAuthError"):
            print("  hint: (re)connect with a valid key: python -m agent composio connect exa")
    if not ok["search"] and resolved.startswith("composio"):
        if s.exa_api_key:
            try:
                d_search, d_scrape, _ = get_tools("direct")
                results = await do_search(d_search)
                ok["search"] = bool(results)
                scrape, used = d_scrape, "direct"
                print(f"search[direct fallback]: {len(results)} results")
            except Exception as e:
                print(f"search[direct fallback] FAILED: {_err(e)}")
        else:
            print("SKIPPED: direct search fallback (EXA_API_KEY missing)")

    url = results[0].url if results else "https://linear.app/developers/graphql"
    page = None
    for label, tool in ((used, scrape), ("direct", get_tools("direct")[1])):
        if label.startswith("composio") and not s.composio_api_key:
            print("SKIPPED: scrape via composio (COMPOSIO_API_KEY missing)")
            continue
        try:
            page = await tool.fetch(url)
            ok["scrape"] = bool(page.text) and page.status < 400
            print(f"scrape[{page.via}]: status={page.status} chars={len(page.text)} url={url}")
            if ok["scrape"]:
                break
        except Exception as e:
            print(f"scrape[{label}] FAILED: {_err(e)}")
        if label == "direct":
            break
        print("  trying direct scrape fallback")

    try:
        if not s.composio_api_key:
            print("SKIPPED: catalog lookup (COMPOSIO_API_KEY missing)")
            raise LookupError
        status, slug = await catalog.has_toolkit("Linear")
        ok["catalog"] = status != "unknown"
        src = getattr(catalog, "source", "composio_catalog")
        print(f"catalog[{src}]: Linear -> {status} ({slug})")
        err = getattr(catalog, "last_error", None)
        if err is not None:
            print(f"catalog FAILED: {_err(err)}")
    except LookupError:
        pass
    except Exception as e:
        print(f"catalog FAILED: {_err(e)}")

    from agent.config import verifier_model_hint

    if hint := verifier_model_hint():
        print(f"WARNING: {hint}")
    if s.meta_api_key:
        try:
            from agent.llm import verify_claim

            r, usage = await verify_claim('Reply with JSON {"ok": true}.', _Ping)
            print(f"meta[{s.verifier_model}]: ok={r.ok} usage={usage}")
        except Exception as e:
            print(f"meta FAILED: {_err(e)}")
    else:
        print("SKIPPED: Meta verifier ping (META_API_KEY missing)")
    if s.openai_api_key:
        try:
            from agent.llm import extract_json

            r, usage = await extract_json("Return JSON.", 'Set ok to true.', _Ping)
            print(f"openai[{s.extractor_model}]: ok={r.ok} usage={usage}")
        except Exception as e:
            print(f"openai FAILED: {_err(e)}")
    else:
        print("SKIPPED: OpenAI extractor ping (OPENAI_API_KEY missing)")

    if ok["search"]:
        try:
            before = get_cache().hits
            await do_search(get_tools("direct" if used == "direct" else None)[0])
            print(f"cache: re-run search {'HIT' if get_cache().hits > before else 'MISS'} {get_cache().stats()}")
        except Exception as e:
            print(f"cache re-run FAILED: {_err(e)}")

    print("Result:", ok)
    return 0 if all(ok.values()) else 1


def run_smoke() -> int:
    try:
        return asyncio.run(_run())
    except Exception as e:  # never traceback
        print(f"smoke aborted: {_err(e)}")
        return 1
