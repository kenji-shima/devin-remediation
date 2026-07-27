import re

import httpx

GITHUB_API_URL = "https://api.github.com"

_PR_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?$")


def parse_pr_url(pr_url: str) -> tuple[str, str, int] | None:
    """Extract (owner, repo, number) from a GitHub PR URL.

    Devin's session response only ever gives us this URL string, never a bare
    PR number -- a Devin-side URL format change would silently break the whole
    downstream loop if this weren't isolated and defensive. Never raises.
    """
    if not pr_url:
        return None
    match = _PR_URL_RE.match(pr_url.strip())
    if not match:
        return None
    owner, repo, number = match.groups()
    return owner, repo, int(number)


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }


async def get_pull_request(
    *, owner: str, repo: str, number: int, token: str, client: httpx.AsyncClient
) -> dict:
    response = await client.get(
        f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{number}",
        headers=_headers(token),
    )
    response.raise_for_status()
    return response.json()


async def list_check_runs(
    *, owner: str, repo: str, sha: str, token: str, client: httpx.AsyncClient
) -> list[dict]:
    response = await client.get(
        f"{GITHUB_API_URL}/repos/{owner}/{repo}/commits/{sha}/check-runs",
        params={"filter": "latest"},
        headers=_headers(token),
    )
    response.raise_for_status()
    return response.json().get("check_runs", [])


async def merge_pull_request(
    *, owner: str, repo: str, number: int, token: str, client: httpx.AsyncClient
) -> dict:
    response = await client.put(
        f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{number}/merge",
        headers=_headers(token),
    )
    response.raise_for_status()
    return response.json()
