import httpx
import respx

from app.github_client import get_pull_request, list_check_runs, merge_pull_request, parse_pr_url


def test_parse_pr_url_extracts_owner_repo_number():
    assert parse_pr_url("https://github.com/kenji-shima/superset/pull/42") == ("kenji-shima", "superset", 42)


def test_parse_pr_url_handles_trailing_slash():
    assert parse_pr_url("https://github.com/kenji-shima/superset/pull/42/") == ("kenji-shima", "superset", 42)


def test_parse_pr_url_returns_none_on_malformed_input():
    assert parse_pr_url("") is None
    assert parse_pr_url("not a url") is None
    assert parse_pr_url("https://github.com/kenji-shima/superset/issues/42") is None
    assert parse_pr_url(None) is None


@respx.mock
async def test_get_pull_request_builds_correct_request():
    respx.get("https://api.github.com/repos/kenji-shima/superset/pulls/42").mock(
        return_value=httpx.Response(200, json={"merged": False, "state": "open", "head": {"sha": "abc123"}})
    )

    async with httpx.AsyncClient() as client:
        result = await get_pull_request(owner="kenji-shima", repo="superset", number=42, token="tok", client=client)

    assert result["head"]["sha"] == "abc123"


@respx.mock
async def test_list_check_runs_requests_latest_filter():
    route = respx.get("https://api.github.com/repos/kenji-shima/superset/commits/abc123/check-runs").mock(
        return_value=httpx.Response(200, json={"check_runs": [{"name": "test", "conclusion": "success"}]})
    )

    async with httpx.AsyncClient() as client:
        result = await list_check_runs(owner="kenji-shima", repo="superset", sha="abc123", token="tok", client=client)

    assert route.called
    assert route.calls.last.request.url.params["filter"] == "latest"
    assert result == [{"name": "test", "conclusion": "success"}]


@respx.mock
async def test_merge_pull_request_builds_correct_request():
    route = respx.put("https://api.github.com/repos/kenji-shima/superset/pulls/42/merge").mock(
        return_value=httpx.Response(200, json={"merged": True, "sha": "def456"})
    )

    async with httpx.AsyncClient() as client:
        result = await merge_pull_request(owner="kenji-shima", repo="superset", number=42, token="tok", client=client)

    assert route.called
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok"
    assert result["merged"] is True
