import httpx
import respx

from app.devin_client import (
    create_playbook,
    create_session,
    get_org_pr_metrics,
    get_org_session_metrics,
    get_session,
    send_message,
)


@respx.mock
async def test_create_session_builds_correct_request():
    route = respx.post("https://api.devin.ai/v3/organizations/org-1/sessions").mock(
        return_value=httpx.Response(
            200, json={"session_id": "devin-abc", "url": "https://app.devin.ai/s/abc", "status": "new"}
        )
    )

    async with httpx.AsyncClient() as client:
        result = await create_session(
            org_id="org-1",
            api_key="test-key",
            prompt="Resolve issue #7",
            tags=["devin-remediation", "issue-7"],
            max_acu_limit=10,
            client=client,
        )

    assert route.called
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer test-key"
    import json

    body = json.loads(request.content)
    assert body["prompt"] == "Resolve issue #7"
    assert body["tags"] == ["devin-remediation", "issue-7"]
    assert body["max_acu_limit"] == 10
    assert body["structured_output_required"] is True
    assert "outcome" in body["structured_output_schema"]["properties"]
    assert "create_as_user_id" not in body
    assert result["session_id"] == "devin-abc"


@respx.mock
async def test_create_session_includes_create_as_user_id_when_given():
    route = respx.post("https://api.devin.ai/v3/organizations/org-1/sessions").mock(
        return_value=httpx.Response(200, json={"session_id": "devin-abc", "status": "new"})
    )

    async with httpx.AsyncClient() as client:
        await create_session(
            org_id="org-1",
            api_key="test-key",
            prompt="Resolve issue #7",
            tags=["devin-remediation", "issue-7"],
            max_acu_limit=10,
            create_as_user_id="user-abc123",
            client=client,
        )

    import json

    body = json.loads(route.calls.last.request.content)
    assert body["create_as_user_id"] == "user-abc123"


@respx.mock
async def test_create_session_includes_playbook_id_when_given():
    route = respx.post("https://api.devin.ai/v3/organizations/org-1/sessions").mock(
        return_value=httpx.Response(200, json={"session_id": "devin-abc", "status": "new"})
    )

    async with httpx.AsyncClient() as client:
        await create_session(
            org_id="org-1",
            api_key="test-key",
            prompt="Resolve issue #7",
            tags=["devin-remediation", "issue-7"],
            max_acu_limit=10,
            playbook_id="playbook-abc",
            client=client,
        )

    import json

    body = json.loads(route.calls.last.request.content)
    assert body["playbook_id"] == "playbook-abc"
    # prompt still carries the per-issue specifics -- playbook_id augments it,
    # never replaces it.
    assert body["prompt"] == "Resolve issue #7"


@respx.mock
async def test_create_playbook_builds_correct_request():
    route = respx.post("https://api.devin.ai/v3/organizations/org-1/playbooks").mock(
        return_value=httpx.Response(200, json={"playbook_id": "playbook-abc", "title": "Remediate a finding"})
    )

    async with httpx.AsyncClient() as client:
        result = await create_playbook(
            org_id="org-1",
            api_key="test-key",
            title="Remediate a finding",
            body="Diagnose before fixing. Run tests until green. Reference the issue number.",
            macro="!remediate-finding",
            client=client,
        )

    assert route.called
    import json

    body = json.loads(route.calls.last.request.content)
    assert body["title"] == "Remediate a finding"
    assert body["macro"] == "!remediate-finding"
    assert result["playbook_id"] == "playbook-abc"


@respx.mock
async def test_get_session_builds_correct_request():
    respx.get("https://api.devin.ai/v3/organizations/org-1/sessions/devin-abc").mock(
        return_value=httpx.Response(200, json={"session_id": "devin-abc", "status": "running"})
    )

    async with httpx.AsyncClient() as client:
        result = await get_session(org_id="org-1", api_key="test-key", devin_session_id="devin-abc", client=client)

    assert result["status"] == "running"


@respx.mock
async def test_get_org_session_metrics_includes_playbook_id_when_given():
    route = respx.get("https://api.devin.ai/v3/organizations/org-1/metrics/sessions").mock(
        return_value=httpx.Response(200, json={"sessions_created_count": 3, "avg_acus_per_session": 1.5})
    )

    async with httpx.AsyncClient() as client:
        result = await get_org_session_metrics(
            org_id="org-1",
            api_key="test-key",
            time_after=1000,
            time_before=2000,
            playbook_id="playbook-abc",
            client=client,
        )

    request = route.calls.last.request
    assert request.url.params["playbook_id"] == "playbook-abc"
    assert request.url.params["time_after"] == "1000"
    assert result["sessions_created_count"] == 3


@respx.mock
async def test_get_org_session_metrics_omits_playbook_id_when_not_given():
    route = respx.get("https://api.devin.ai/v3/organizations/org-1/metrics/sessions").mock(
        return_value=httpx.Response(200, json={"sessions_created_count": 3})
    )

    async with httpx.AsyncClient() as client:
        await get_org_session_metrics(
            org_id="org-1", api_key="test-key", time_after=1000, time_before=2000, client=client
        )

    assert "playbook_id" not in route.calls.last.request.url.params


@respx.mock
async def test_get_org_pr_metrics_builds_correct_request():
    route = respx.get("https://api.devin.ai/v3/organizations/org-1/metrics/prs").mock(
        return_value=httpx.Response(
            200,
            json={"prs_created_count": 2, "prs_opened_count": 0, "prs_merged_count": 1, "prs_closed_count": 1},
        )
    )

    async with httpx.AsyncClient() as client:
        result = await get_org_pr_metrics(
            org_id="org-1",
            api_key="test-key",
            time_after=1000,
            time_before=2000,
            playbook_id="playbook-abc",
            client=client,
        )

    assert route.calls.last.request.url.params["playbook_id"] == "playbook-abc"
    assert result["prs_merged_count"] == 1
    assert result["prs_closed_count"] == 1


@respx.mock
async def test_send_message_builds_correct_request():
    route = respx.post("https://api.devin.ai/v3/organizations/org-1/sessions/devin-abc/messages").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    async with httpx.AsyncClient() as client:
        await send_message(
            org_id="org-1", api_key="test-key", devin_session_id="devin-abc", message="CI is red", client=client
        )

    assert route.called
    import json

    body = json.loads(route.calls.last.request.content)
    assert body["message"] == "CI is red"
