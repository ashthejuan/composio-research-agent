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


def test_official_domains_not_too_broad():
    from agent.registry import load_apps

    broad = {"amazonaws.com", "netlify.app", "microsoft.com"}
    for a in load_apps():
        assert not broad & set(a.official_domains), a.name
