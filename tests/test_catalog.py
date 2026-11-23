import asyncio

from agent.tools import composio_tools as ct
from agent.tools.composio_tools import DocsCatalog, FallbackCatalog, parse_docs_slugs, slug_candidates

HTML = '''<a href="/toolkits/github">x</a><a href="/toolkits/_1password">y</a>
<a href="/toolkits/zoho_cliq?x=1">z</a><a href="/toolkits/notebook_lm">n</a>
<a href="/toolkits/GitHub">dup</a><a href="/toolkits/google-ads/">tail</a> <a href="/toolkits">none</a>'''


def run(c):
    return asyncio.run(c)


def _docs(slugs):
    d = DocsCatalog()

    async def fake():
        return slugs
    d._fetch = fake
    return d


def test_parse_slugs():
    s = parse_docs_slugs(HTML)
    assert s == ["github", "_1password", "zoho_cliq", "notebook_lm", "google-ads"]
    assert parse_docs_slugs('<a href="/toolkits/x/tools/y">a</a> /toolkits/logo.png "/toolkits/ok"') == ["ok"]


def test_normalisation_variants(monkeypatch):
    monkeypatch.setattr(ct, "get_cache", lambda: _MemCache())
    d = _docs(["notebook_lm", "_1password"])
    assert run(d.has_toolkit("NotebookLM")) == ("available", "notebook_lm")
    assert run(d.has_toolkit("1Password")) == ("available", "_1password")


def test_exact_match_no_substring(monkeypatch):
    monkeypatch.setattr(ct, "get_cache", lambda: _MemCache())
    d = _docs(["salesforce_data_360", "zoho_cliq", "pylon_mcp"])
    assert run(d.has_toolkit("Salesforce")) == ("unavailable", None)
    assert run(d.has_toolkit("Zoho")) == ("unavailable", None)
    assert run(d.has_toolkit("Pylon")) == ("available", "pylon_mcp")  # via alias, exact


def test_commerce_cloud_not_salesforce():
    assert "salesforce" not in slug_candidates("Salesforce Commerce Cloud")


def test_docs_unknown_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(ct, "get_cache", lambda: _MemCache())
    d = DocsCatalog()

    async def boom():
        raise RuntimeError("down")
    d._fetch = boom
    assert run(d.has_toolkit("Slack")) == ("unknown", None)


class _Fake:
    def __init__(self, res):
        self.res = res

    async def has_toolkit(self, app):
        return self.res


def test_fallback_uses_secondary_and_reports_source():
    f = FallbackCatalog(_Fake(("unknown", None)), _Fake(("available", "slack")))
    assert run(f.lookup("Slack")) == ("available", "slack", "composio_docs_catalog")
    assert run(f.has_toolkit("Slack")) == ("available", "slack")
    f2 = FallbackCatalog(_Fake(("unavailable", None)), _Fake(("available", "x")))
    assert run(f2.lookup("Foo")) == ("unavailable", None, "composio_catalog")


class _MemCache:
    def __init__(self):
        self.d = {}

    def get(self, ns, args):
        return None

    def set(self, ns, args, v):
        self.d[ns] = v


def test_amazon_selling_partner_not_generic_amazon():
    assert "amazon" not in slug_candidates("Amazon Selling Partner")
