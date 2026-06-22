from mcp_geo_server.formatting import as_list, extract, unwrap


def test_as_list_empty_variants():
    assert as_list(None) == []
    assert as_list("") == []
    assert as_list([]) == []


def test_as_list_singleton_and_list():
    assert as_list({"a": 1}) == [{"a": 1}]
    assert as_list([1, 2]) == [1, 2]


def test_extract_double_wrapped_list():
    data = {"workspaces": {"workspace": [{"name": "a"}, {"name": "b"}]}}
    assert extract(data, "workspaces", "workspace") == [{"name": "a"}, {"name": "b"}]


def test_extract_singleton():
    data = {"workspaces": {"workspace": {"name": "solo"}}}
    assert extract(data, "workspaces", "workspace") == [{"name": "solo"}]


def test_extract_empty_string_means_empty():
    assert extract({"workspaces": ""}, "workspaces", "workspace") == []
    assert extract({}, "workspaces", "workspace") == []


def test_unwrap():
    assert unwrap({"workspace": {"name": "x"}}, "workspace") == {"name": "x"}
    assert unwrap({"other": 1}, "workspace") == {"other": 1}
