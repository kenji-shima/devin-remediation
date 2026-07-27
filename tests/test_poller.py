import httpx
from sqlalchemy import select

import app.poller as poller
from app.config import get_settings
from app.metrics import compute_mttr
from app.models import DevinSession, Issue, SessionPrState, Transition, utcnow

PR_URL = "https://github.com/kenji-shima/devin-remediation-webhook-test/pull/1"


def _make_session(db_session, github_issue_id: int, *, acu_cap: float = 10.0, created_at=None) -> DevinSession:
    issue = Issue(
        github_issue_id=github_issue_id,
        github_issue_number=github_issue_id,
        repo_owner="kenji-shima",
        repo_name="devin-remediation-webhook-test",
        title="requests==2.27.1 has known CVEs",
        body="fix it",
        label="devin-fix",
        state="open",
        github_created_at=utcnow(),
    )
    db_session.add(issue)
    db_session.flush()
    session = DevinSession(
        issue_id=issue.id,
        devin_session_id=f"devin-{github_issue_id}",
        tag=f"issue-{github_issue_id}",
        status="running",
        acu_cap=acu_cap,
        lifecycle_stage="active",
        created_at=created_at or utcnow(),
    )
    db_session.add(session)
    db_session.commit()
    return session


def _patch_poller(monkeypatch, *, get_session=None, send_message=None, get_pr=None, check_runs=None, merge=None):
    if get_session is not None:
        monkeypatch.setattr(poller, "devin_get_session", get_session)
    if send_message is not None:
        monkeypatch.setattr(poller, "devin_send_message", send_message)
    if get_pr is not None:
        monkeypatch.setattr(poller, "get_pull_request", get_pr)
    if check_runs is not None:
        monkeypatch.setattr(poller, "list_check_runs", check_runs)
    if merge is not None:
        monkeypatch.setattr(poller, "merge_pull_request", merge)


async def _run(session_id: int):
    async with httpx.AsyncClient() as client:
        await poller.poll_one_session(session_id, get_settings(), client)


def test_compute_ci_outcome_empty_is_no_ci_not_pass():
    outcome, sig = poller.compute_ci_outcome([])
    assert outcome == "no_ci"
    assert sig is None


def test_compute_ci_outcome_pending():
    outcome, _ = poller.compute_ci_outcome([{"status": "in_progress", "conclusion": None}])
    assert outcome == "pending"


def test_compute_ci_outcome_pass_and_fail():
    assert poller.compute_ci_outcome([{"status": "completed", "conclusion": "success"}])[0] == "pass"
    outcome, sig = poller.compute_ci_outcome([{"status": "completed", "conclusion": "failure", "name": "pytest"}])
    assert outcome == "fail"
    assert sig is not None


async def test_pr_opened_recorded_once_across_ticks(patched_session_locals, db_session, monkeypatch):
    session = _make_session(db_session, 6001)

    async def get_session(**kwargs):
        return {
            "status": "running",
            "status_detail": "working",
            "acus_consumed": 1.0,
            "pull_requests": [{"pr_url": PR_URL, "pr_state": "open"}],
            "structured_output": None,
        }

    async def get_pr(**kwargs):
        return {"merged": False, "state": "open", "head": {"sha": "sha-1"}}

    async def check_runs(**kwargs):
        return []  # no_ci

    _patch_poller(monkeypatch, get_session=get_session, get_pr=get_pr, check_runs=check_runs)

    await _run(session.id)
    await _run(session.id)

    pr_states = db_session.execute(select(SessionPrState).where(SessionPrState.session_id == session.id)).scalars().all()
    assert len(pr_states) == 1
    opened = db_session.execute(
        select(Transition).where(Transition.session_id == session.id, Transition.type == "pr_opened")
    ).scalars().all()
    assert len(opened) == 1
    # Regression: MTTR joins issue_created/merged transitions on issue_id --
    # every poller-recorded transition must carry it, not just session_id.
    assert opened[0].issue_id == session.issue_id


async def test_empty_check_runs_is_no_ci_never_merges(patched_session_locals, db_session, monkeypatch):
    session = _make_session(db_session, 6002)
    merge_called = {"n": 0}

    async def get_session(**kwargs):
        return {
            "status": "running", "status_detail": "working", "acus_consumed": 1.0,
            "pull_requests": [{"pr_url": PR_URL}], "structured_output": None,
        }

    async def get_pr(**kwargs):
        return {"merged": False, "state": "open", "head": {"sha": "sha-1"}}

    async def check_runs(**kwargs):
        return []

    async def merge(**kwargs):
        merge_called["n"] += 1
        return {"merged": True, "sha": "merge-sha"}

    _patch_poller(monkeypatch, get_session=get_session, get_pr=get_pr, check_runs=check_runs, merge=merge)

    await _run(session.id)

    assert merge_called["n"] == 0
    merged = db_session.execute(
        select(Transition).where(Transition.session_id == session.id, Transition.type == "merged")
    ).scalars().first()
    assert merged is None


async def test_ci_fail_records_transition_and_sends_message_once(patched_session_locals, db_session, monkeypatch):
    session = _make_session(db_session, 6003)
    message_calls = {"n": 0}
    check_runs_calls = {"n": 0}

    async def get_session(**kwargs):
        return {
            "status": "running", "status_detail": "working", "acus_consumed": 1.0,
            "pull_requests": [{"pr_url": PR_URL}], "structured_output": None,
        }

    async def get_pr(**kwargs):
        return {"merged": False, "state": "open", "head": {"sha": "sha-1"}}

    async def check_runs(**kwargs):
        check_runs_calls["n"] += 1
        return [{"name": "pytest", "status": "completed", "conclusion": "failure"}]

    async def send_message(**kwargs):
        message_calls["n"] += 1
        return {"status": "ok"}

    _patch_poller(monkeypatch, get_session=get_session, get_pr=get_pr, check_runs=check_runs, send_message=send_message)

    await _run(session.id)
    await _run(session.id)  # same sha, same failure -- must not re-fetch, re-send, or re-record

    assert check_runs_calls["n"] == 1
    assert message_calls["n"] == 1
    ci_results = db_session.execute(
        select(Transition).where(Transition.session_id == session.id, Transition.type == "ci_result")
    ).scalars().all()
    assert len(ci_results) == 1
    assert ci_results[0].outcome == "fail"


async def test_call_with_retry_does_not_retry_4xx(monkeypatch):
    monkeypatch.setattr(poller.asyncio, "sleep", lambda *_: None)
    calls = {"n": 0}
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(403, request=request)

    async def flaky(**kwargs):
        calls["n"] += 1
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    try:
        await poller.call_with_retry(flaky)
    except poller.ApiError:
        pass
    else:
        raise AssertionError("expected ApiError")

    assert calls["n"] == 1


async def test_call_with_retry_retries_5xx_up_to_attempt_limit(monkeypatch):
    async def no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(poller.asyncio, "sleep", no_sleep)
    calls = {"n": 0}
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(503, request=request)

    async def flaky(**kwargs):
        calls["n"] += 1
        raise httpx.HTTPStatusError("unavailable", request=request, response=response)

    try:
        await poller.call_with_retry(flaky)
    except poller.ApiError:
        pass
    else:
        raise AssertionError("expected ApiError")

    assert calls["n"] == 3


async def test_ci_pass_merges_and_records_once(patched_session_locals, db_session, monkeypatch):
    session = _make_session(db_session, 6004)
    merge_calls = {"n": 0}

    async def get_session(**kwargs):
        return {
            "status": "running", "status_detail": "working", "acus_consumed": 1.0,
            "pull_requests": [{"pr_url": PR_URL}], "structured_output": None,
        }

    async def get_pr(**kwargs):
        return {"merged": False, "state": "open", "head": {"sha": "sha-2"}}

    async def check_runs(**kwargs):
        return [{"name": "pytest", "status": "completed", "conclusion": "success"}]

    async def merge(**kwargs):
        merge_calls["n"] += 1
        return {"merged": True, "sha": "merge-sha"}

    _patch_poller(monkeypatch, get_session=get_session, get_pr=get_pr, check_runs=check_runs, merge=merge)

    await _run(session.id)

    assert merge_calls["n"] == 1
    db_session.expire_all()
    refreshed = db_session.get(DevinSession, session.id)
    assert refreshed.goal_achieved is True
    assert refreshed.lifecycle_stage == "done"
    merged = db_session.execute(
        select(Transition).where(Transition.session_id == session.id, Transition.type == "merged")
    ).scalars().all()
    assert len(merged) == 1
    assert merged[0].issue_id == session.issue_id


async def test_already_merged_pr_detected_before_merge_call(patched_session_locals, db_session, monkeypatch):
    session = _make_session(db_session, 6005)
    merge_calls = {"n": 0}

    async def get_session(**kwargs):
        return {
            "status": "exit", "status_detail": "finished", "acus_consumed": 1.0,
            "pull_requests": [{"pr_url": PR_URL}],
            "structured_output": {"outcome": "fixed", "pr_url": PR_URL, "notes": "done"},
        }

    async def get_pr(**kwargs):
        return {"merged": True, "state": "closed", "head": {"sha": "sha-3"}}

    async def merge(**kwargs):
        merge_calls["n"] += 1
        return {"merged": True, "sha": "merge-sha"}

    _patch_poller(monkeypatch, get_session=get_session, get_pr=get_pr, merge=merge)

    await _run(session.id)

    assert merge_calls["n"] == 0  # already merged -- never call merge
    db_session.expire_all()
    refreshed = db_session.get(DevinSession, session.id)
    assert refreshed.goal_achieved is True
    assert refreshed.lifecycle_stage == "done"


async def test_closed_without_merge_stops_ci_work(patched_session_locals, db_session, monkeypatch):
    session = _make_session(db_session, 6006)
    check_runs_calls = {"n": 0}

    async def get_session(**kwargs):
        return {
            "status": "running", "status_detail": "working", "acus_consumed": 1.0,
            "pull_requests": [{"pr_url": PR_URL}], "structured_output": None,
        }

    async def get_pr(**kwargs):
        return {"merged": False, "state": "closed", "head": {"sha": "sha-4"}}

    async def check_runs(**kwargs):
        check_runs_calls["n"] += 1
        return []

    _patch_poller(monkeypatch, get_session=get_session, get_pr=get_pr, check_runs=check_runs)

    await _run(session.id)
    await _run(session.id)

    assert check_runs_calls["n"] == 0
    pr_state = db_session.execute(
        select(SessionPrState).where(SessionPrState.session_id == session.id)
    ).scalars().first()
    assert pr_state.pr_resolved_without_merge is True


async def test_terminal_resolution_skipped_when_pr_unresolved(patched_session_locals, db_session, monkeypatch):
    session = _make_session(db_session, 6007)

    async def get_session(**kwargs):
        return {
            "status": "exit", "status_detail": "finished", "acus_consumed": 1.0,
            "pull_requests": [{"pr_url": PR_URL}],
            "structured_output": {"outcome": "fixed", "pr_url": PR_URL, "notes": "done"},
        }

    async def get_pr(**kwargs):
        return {"merged": False, "state": "open", "head": {"sha": "sha-5"}}

    async def check_runs(**kwargs):
        return []  # no_ci -> any_pr_unresolved=True -> terminal resolution must NOT fire

    _patch_poller(monkeypatch, get_session=get_session, get_pr=get_pr, check_runs=check_runs)

    await _run(session.id)

    db_session.expire_all()
    refreshed = db_session.get(DevinSession, session.id)
    assert refreshed.goal_achieved is None
    assert refreshed.lifecycle_stage == "active"


async def test_terminal_resolution_exit_without_fixed_outcome_is_failure(patched_session_locals, db_session, monkeypatch):
    session = _make_session(db_session, 6008)

    async def get_session(**kwargs):
        return {
            "status": "exit", "status_detail": "finished", "acus_consumed": 1.0,
            "pull_requests": [], "structured_output": {"outcome": "blocked", "pr_url": None, "notes": "n/a"},
        }

    _patch_poller(monkeypatch, get_session=get_session)

    await _run(session.id)

    db_session.expire_all()
    refreshed = db_session.get(DevinSession, session.id)
    assert refreshed.goal_achieved is False
    assert refreshed.lifecycle_stage == "done"


async def test_terminal_resolution_on_structured_output_without_exit_status(
    patched_session_locals, db_session, monkeypatch
):
    """Regression: a triage-only run (playbook false-positive dismissal, no
    PR) can report structured_output while status is still "running" /
    status_detail "waiting_for_user" -- this must resolve as done rather than
    polling forever until a misleading max_age escalation."""
    session = _make_session(db_session, 6013)

    async def get_session(**kwargs):
        return {
            "status": "running", "status_detail": "waiting_for_user", "acus_consumed": 0.5,
            "pull_requests": [],
            "structured_output": {"outcome": "false_positive", "pr_url": None, "notes": "no sink, read-only"},
        }

    _patch_poller(monkeypatch, get_session=get_session)

    await _run(session.id)

    db_session.expire_all()
    refreshed = db_session.get(DevinSession, session.id)
    assert refreshed.goal_achieved is False
    assert refreshed.lifecycle_stage == "done"


async def test_escalation_on_acu_ratio(patched_session_locals, db_session, monkeypatch):
    session = _make_session(db_session, 6009, acu_cap=10.0)

    async def get_session(**kwargs):
        return {
            "status": "running", "status_detail": "working", "acus_consumed": 9.0,
            "pull_requests": [], "structured_output": None,
        }

    _patch_poller(monkeypatch, get_session=get_session)

    await _run(session.id)

    db_session.expire_all()
    refreshed = db_session.get(DevinSession, session.id)
    assert refreshed.lifecycle_stage == "escalated"
    assert refreshed.escalated_reason == "acu_ratio"


async def test_escalation_on_hard_stop_status_detail(patched_session_locals, db_session, monkeypatch):
    session = _make_session(db_session, 6010)

    async def get_session(**kwargs):
        return {
            "status": "suspended", "status_detail": "out_of_credits", "acus_consumed": 1.0,
            "pull_requests": [], "structured_output": None,
        }

    _patch_poller(monkeypatch, get_session=get_session)

    await _run(session.id)

    db_session.expire_all()
    refreshed = db_session.get(DevinSession, session.id)
    assert refreshed.lifecycle_stage == "escalated"
    assert refreshed.escalated_reason == "quota_exhausted"


async def test_run_one_tick_one_session_failure_does_not_block_others(patched_session_locals, db_session, monkeypatch):
    async def no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(poller.asyncio, "sleep", no_sleep)
    good = _make_session(db_session, 6011)
    bad = _make_session(db_session, 6012)

    async def get_session(**kwargs):
        if kwargs.get("devin_session_id") == bad.devin_session_id:
            raise httpx.ConnectError("boom")
        return {
            "status": "running", "status_detail": "working", "acus_consumed": 1.0,
            "pull_requests": [], "structured_output": None,
        }

    _patch_poller(monkeypatch, get_session=get_session)

    async with httpx.AsyncClient() as client:
        await poller.run_one_tick(get_settings(), client)

    db_session.expire_all()
    good_refreshed = db_session.get(DevinSession, good.id)
    bad_refreshed = db_session.get(DevinSession, bad.id)
    assert good_refreshed.last_polled_at is not None
    assert bad_refreshed.consecutive_poll_errors == 1


async def test_mttr_computes_after_real_merge_flow(patched_session_locals, db_session, monkeypatch):
    """End-to-end regression for the issue_id-on-transitions bug: MTTR joins
    issue_created and merged transitions by issue_id, so every transition the
    poller writes must carry it, not just session_id."""
    session = _make_session(db_session, 6013)
    db_session.add(
        Transition(issue_id=session.issue_id, type="issue_created", occurred_at=session.created_at)
    )
    db_session.commit()

    async def get_session(**kwargs):
        return {
            "status": "running", "status_detail": "working", "acus_consumed": 1.0,
            "pull_requests": [{"pr_url": PR_URL}], "structured_output": None,
        }

    async def get_pr(**kwargs):
        return {"merged": False, "state": "open", "head": {"sha": "sha-9"}}

    async def check_runs(**kwargs):
        return [{"name": "pytest", "status": "completed", "conclusion": "success"}]

    async def merge(**kwargs):
        return {"merged": True, "sha": "merge-sha"}

    _patch_poller(monkeypatch, get_session=get_session, get_pr=get_pr, check_runs=check_runs, merge=merge)

    await _run(session.id)

    assert compute_mttr(db_session) is not None
