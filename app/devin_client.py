import httpx

DEVIN_BASE_URL = "https://api.devin.ai"

STRUCTURED_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["fixed", "false_positive", "blocked"]},
        "pr_url": {"type": ["string", "null"]},
        "notes": {"type": "string"},
    },
    "required": ["outcome", "notes"],
}


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


async def create_session(
    *,
    org_id: str,
    api_key: str,
    prompt: str,
    tags: list[str],
    max_acu_limit: float,
    create_as_user_id: str | None = None,
    playbook_id: str | None = None,
    client: httpx.AsyncClient,
) -> dict:
    body = {
        "prompt": prompt,
        "tags": tags,
        "max_acu_limit": max_acu_limit,
        "structured_output_schema": STRUCTURED_OUTPUT_SCHEMA,
        "structured_output_required": True,
    }
    if create_as_user_id:
        # Attributes the session to this human user in the Devin dashboard
        # instead of the API key's own service-user/bot identity.
        body["create_as_user_id"] = create_as_user_id
    if playbook_id:
        # Reusable operating procedure (diagnose before fixing, run tests
        # until green, reference the issue number). `prompt` still carries
        # the per-issue specifics -- the playbook isn't a variable-templating
        # engine, it's durable doctrine layered under the per-request prompt.
        body["playbook_id"] = playbook_id

    response = await client.post(
        f"{DEVIN_BASE_URL}/v3/organizations/{org_id}/sessions",
        json=body,
        headers=_headers(api_key),
    )
    response.raise_for_status()
    return response.json()


async def create_playbook(
    *,
    org_id: str,
    api_key: str,
    title: str,
    body: str,
    macro: str | None = None,
    client: httpx.AsyncClient,
) -> dict:
    payload = {"title": title, "body": body}
    if macro:
        payload["macro"] = macro

    response = await client.post(
        f"{DEVIN_BASE_URL}/v3/organizations/{org_id}/playbooks",
        json=payload,
        headers=_headers(api_key),
    )
    response.raise_for_status()
    return response.json()


async def get_session(
    *,
    org_id: str,
    api_key: str,
    devin_session_id: str,
    client: httpx.AsyncClient,
) -> dict:
    response = await client.get(
        f"{DEVIN_BASE_URL}/v3/organizations/{org_id}/sessions/{devin_session_id}",
        headers=_headers(api_key),
    )
    response.raise_for_status()
    return response.json()


async def send_message(
    *,
    org_id: str,
    api_key: str,
    devin_session_id: str,
    message: str,
    client: httpx.AsyncClient,
) -> dict:
    response = await client.post(
        f"{DEVIN_BASE_URL}/v3/organizations/{org_id}/sessions/{devin_session_id}/messages",
        json={"message": message},
        headers=_headers(api_key),
    )
    response.raise_for_status()
    return response.json()


async def get_org_session_metrics(
    *,
    org_id: str,
    api_key: str,
    time_after: int,
    time_before: int,
    playbook_id: str | None = None,
    client: httpx.AsyncClient,
) -> dict:
    """GET /v3/organizations/{org_id}/metrics/sessions -- includes
    avg_acus_per_session, the org-level cost signal from Devin's own side.

    playbook_id is verified live to actually scope results (unlike the
    `tags` filter on the sessions-list endpoint, which is documented but
    does not filter at all) -- passing our own playbook id here is what
    makes this safe to show without picking up unrelated org activity.
    """
    params = {"time_after": time_after, "time_before": time_before}
    if playbook_id:
        params["playbook_id"] = playbook_id
    response = await client.get(
        f"{DEVIN_BASE_URL}/v3/organizations/{org_id}/metrics/sessions",
        params=params,
        headers=_headers(api_key),
    )
    response.raise_for_status()
    return response.json()


async def get_org_pr_metrics(
    *,
    org_id: str,
    api_key: str,
    time_after: int,
    time_before: int,
    playbook_id: str | None = None,
    client: httpx.AsyncClient,
) -> dict:
    """GET /v3/organizations/{org_id}/metrics/prs -- created/opened/merged/
    closed-without-merging counts. Same verified-working playbook_id scoping
    as get_org_session_metrics.
    """
    params = {"time_after": time_after, "time_before": time_before}
    if playbook_id:
        params["playbook_id"] = playbook_id
    response = await client.get(
        f"{DEVIN_BASE_URL}/v3/organizations/{org_id}/metrics/prs",
        params=params,
        headers=_headers(api_key),
    )
    response.raise_for_status()
    return response.json()
