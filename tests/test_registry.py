from agent.registry import check_registry, get_app, is_official_url, select


def test_check_registry():
    assert check_registry() == 0


def test_get_app():
    assert get_app("hubspot").id == 2
    assert get_app(2).name == "HubSpot"


def test_select_ranges():
    assert [a.id for a in select("1-3,5")] == [1, 2, 3, 5]
    assert len(select(None, "Ecommerce")) == 10


def test_official_url():
    app = get_app("HubSpot")
    assert is_official_url(app, "https://developers.hubspot.com/docs/api/overview")
    assert not is_official_url(app, "https://example.com/hubspot.com")
    assert not is_official_url(app, "https://evilhubspot.com/docs")


def test_path_prefix_domain():
    app = get_app("Sherlock")
    assert is_official_url(app, "https://github.com/sherlock-project/sherlock")
    assert not is_official_url(app, "https://github.com/other/repo")


def test_github_org_match_is_case_insensitive():
    app = get_app("HubSpot")  # registry entry "github.com/HubSpot"
    assert is_official_url(app, "https://github.com/hubspot/hubspot-api-python")
    assert is_official_url(app, "https://github.com/HubSpot/hubspot-api-python")


def test_bare_code_host_does_not_make_every_repo_official():
    from agent.research import source_tier

    app = get_app("GitHub")  # official_domains include bare "github.com" and "github.com/github"
    assert not is_official_url(app, "https://github.com/randomuser/github-mcp")
    assert source_tier(app, "https://github.com/randomuser/github-mcp") == "community"
    assert source_tier(app, "https://github.com/github/github-mcp-server") == "official_github"
    assert is_official_url(app, "https://github.com/pricing")  # the site's own pages stay official
    assert is_official_url(app, "https://docs.github.com/en/rest")


def test_official_domains_not_too_broad():
    from agent.registry import load_apps

    broad = {"amazonaws.com", "netlify.app", "microsoft.com"}
    for a in load_apps():
        assert not broad & set(a.official_domains), a.name
