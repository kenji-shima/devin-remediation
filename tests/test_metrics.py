from datetime import datetime, timedelta

import httpx
import respx

from app.config import get_settings
from app.models import DevinSession, Issue, SessionPrState, Transition, utcnow

BASE_TIME = datetime(2026, 7, 1)


def _make_issue(db_session, github_issue_id: int, **overrides) -> Issue:
    defaults = dict(
        github_issue_id=github_issue_id,
        github_issue_number=github_issue_id,
        repo_owner="kenji-shima",
        repo_name="superset",
        title="finding",
        body="body",
        label="devin-fix",
        state="open",
        github_created_at=BASE_TIME,
    )
    defaults.update(overrides)
    issue = Issue(**defaults)
    db_session.add(issue)
    db_session.flush()
    return issue


def test_metrics_summary_empty_store(client):
    resp = client.get("/metrics/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mttr_hours"] is None
    assert data["merge_rate"] is None
    assert data["success_rate"] is None
    assert data["throughput_per_day"] is None
    assert data["cost_acu"] is None
    assert data["cost_per_fix_acu"] is None
    assert data["avg_acu_per_session"] is None
    assert data["issues_opened"] == 0
    assert data["issues_merged"] == 0
    assert data["sessions_total"] == 0
    assert data["triage_dismissals"] == 0
    assert data["devin_org_metrics"] is None


def test_metrics_timeseries_empty_store(client):
    resp = client.get("/metrics/timeseries")
    assert resp.status_code == 200
    assert resp.json() == []


def test_mttr_computation(client, db_session):
    issue = _make_issue(db_session, 2001)
    db_session.add(Transition(issue_id=issue.id, type="issue_created", occurred_at=BASE_TIME))
    db_session.add(
        Transition(
            issue_id=issue.id,
            type="merged",
            occurred_at=BASE_TIME + timedelta(hours=10),
        )
    )
    db_session.commit()

    resp = client.get("/metrics/summary")
    assert resp.json()["mttr_hours"] == 10.0


def test_merge_rate_computation(client, db_session):
    issue_merged = _make_issue(db_session, 2002)
    issue_open = _make_issue(db_session, 2003)
    db_session.add(Transition(issue_id=issue_merged.id, type="issue_created", occurred_at=BASE_TIME))
    db_session.add(Transition(issue_id=issue_open.id, type="issue_created", occurred_at=BASE_TIME))
    db_session.add(
        Transition(issue_id=issue_merged.id, type="merged", occurred_at=BASE_TIME + timedelta(hours=1))
    )
    db_session.commit()

    resp = client.get("/metrics/summary")
    data = resp.json()
    assert data["issues_opened"] == 2
    assert data["issues_merged"] == 1
    assert data["merge_rate"] == 0.5


def test_success_rate_computation(client, db_session):
    issue = _make_issue(db_session, 2004)
    db_session.add(DevinSession(issue_id=issue.id, status="finished", goal_achieved=True, outcome="fixed"))
    db_session.add(DevinSession(issue_id=issue.id, status="error", goal_achieved=False, outcome="blocked"))
    db_session.commit()

    resp = client.get("/metrics/summary")
    assert resp.json()["success_rate"] == 0.5
    assert resp.json()["sessions_total"] == 2


def test_success_rate_excludes_false_positive_dismissals(client, db_session):
    """A correctly-diagnosed false positive must not count as a remediation
    failure -- it's excluded from the denominator entirely, not counted
    against success_rate."""
    issue = _make_issue(db_session, 2007)
    db_session.add(DevinSession(issue_id=issue.id, status="exit", goal_achieved=True, outcome="fixed"))
    db_session.add(DevinSession(issue_id=issue.id, status="running", goal_achieved=False, outcome="false_positive"))
    db_session.commit()

    resp = client.get("/metrics/summary")
    data = resp.json()
    assert data["success_rate"] == 1.0
    assert data["triage_dismissals"] == 1
    assert data["sessions_total"] == 2


def test_throughput_computation(client, db_session):
    issue = _make_issue(db_session, 2005)
    now = utcnow()
    db_session.add(Transition(issue_id=issue.id, type="issue_created", occurred_at=now))
    db_session.add(Transition(issue_id=issue.id, type="merged", occurred_at=now - timedelta(days=1)))
    db_session.add(Transition(issue_id=issue.id, type="merged", occurred_at=now - timedelta(days=2)))
    db_session.commit()

    resp = client.get("/metrics/summary")
    assert resp.json()["throughput_per_day"] == 2 / 7


def test_cost_computation(client, db_session):
    issue = _make_issue(db_session, 2006)
    db_session.add(DevinSession(issue_id=issue.id, status="finished", acu_used=3.5))
    db_session.add(DevinSession(issue_id=issue.id, status="finished", acu_used=2.5))
    db_session.commit()

    resp = client.get("/metrics/summary")
    assert resp.json()["cost_acu"] == 6.0


def test_avg_acu_per_session_computation(client, db_session):
    issue = _make_issue(db_session, 2010)
    db_session.add(DevinSession(issue_id=issue.id, status="finished", acu_used=3.0))
    db_session.add(DevinSession(issue_id=issue.id, status="finished", acu_used=1.0))
    db_session.commit()

    resp = client.get("/metrics/summary")
    assert resp.json()["avg_acu_per_session"] == 2.0


@respx.mock
def test_devin_org_metrics_included_when_configured(client, monkeypatch):
    monkeypatch.setenv("DEVIN_API_KEY", "test-key")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-1")
    monkeypatch.setenv("DEVIN_PLAYBOOK_ID", "playbook-abc")
    get_settings.cache_clear()
    try:
        sessions_route = respx.get("https://api.devin.ai/v3/organizations/org-1/metrics/sessions").mock(
            return_value=httpx.Response(
                200, json={"sessions_created_count": 5, "avg_acus_per_session": 2.5}
            )
        )
        prs_route = respx.get("https://api.devin.ai/v3/organizations/org-1/metrics/prs").mock(
            return_value=httpx.Response(
                200,
                json={
                    "prs_created_count": 4,
                    "prs_opened_count": 0,
                    "prs_merged_count": 3,
                    "prs_closed_count": 1,
                },
            )
        )

        resp = client.get("/metrics/summary")

        assert resp.status_code == 200
        devin_metrics = resp.json()["devin_org_metrics"]
        assert devin_metrics == {
            "sessions_count": 5,
            "prs_created_count": 4,
            "prs_merged_count": 3,
            "prs_closed_count": 1,
            "avg_acus_per_session": 2.5,
        }
        # Both calls must actually carry our playbook_id -- an unscoped call
        # would silently pick up unrelated org activity (see fetch_devin_org_metrics).
        assert sessions_route.calls.last.request.url.params["playbook_id"] == "playbook-abc"
        assert prs_route.calls.last.request.url.params["playbook_id"] == "playbook-abc"
    finally:
        get_settings.cache_clear()


def test_devin_org_metrics_none_without_playbook_configured(client, monkeypatch):
    """Even with API key + org id set, no playbook_id means no verified way
    to scope the call -- must return None rather than show unscoped data."""
    monkeypatch.setenv("DEVIN_API_KEY", "test-key")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-1")
    monkeypatch.setenv("DEVIN_PLAYBOOK_ID", "")
    get_settings.cache_clear()
    try:
        resp = client.get("/metrics/summary")
        assert resp.json()["devin_org_metrics"] is None
    finally:
        get_settings.cache_clear()


def test_cost_per_fix_none_before_any_merge(client, db_session):
    issue = _make_issue(db_session, 2008)
    db_session.add(DevinSession(issue_id=issue.id, status="running", acu_used=3.0))
    db_session.commit()

    resp = client.get("/metrics/summary")
    assert resp.json()["cost_per_fix_acu"] is None


def test_cost_per_fix_computation(client, db_session):
    issue = _make_issue(db_session, 2009)
    db_session.add(Transition(issue_id=issue.id, type="issue_created", occurred_at=BASE_TIME))
    db_session.add(
        Transition(issue_id=issue.id, type="merged", occurred_at=BASE_TIME + timedelta(hours=1))
    )
    db_session.add(DevinSession(issue_id=issue.id, status="finished", acu_used=4.0))
    db_session.commit()

    resp = client.get("/metrics/summary")
    assert resp.json()["cost_per_fix_acu"] == 4.0


def test_summary_excludes_other_repo(client, db_session):
    """A calibration/test repo used during development must never leak into
    the numbers shown for the configured target repo (GITHUB_REPO_OWNER/
    GITHUB_REPO_NAME, set to kenji-shima/superset by the client fixture)."""
    target = _make_issue(db_session, 4001, github_issue_number=1)
    db_session.add(Transition(issue_id=target.id, type="issue_created", occurred_at=BASE_TIME))
    db_session.add(
        Transition(issue_id=target.id, type="merged", occurred_at=BASE_TIME + timedelta(hours=1))
    )
    db_session.add(DevinSession(issue_id=target.id, status="finished", acu_used=2.0))

    other = _make_issue(
        db_session, 4002, github_issue_number=1,
        repo_owner="kenji-shima", repo_name="devin-remediation-webhook-test",
    )
    db_session.add(Transition(issue_id=other.id, type="issue_created", occurred_at=BASE_TIME))
    db_session.add(
        Transition(issue_id=other.id, type="merged", occurred_at=BASE_TIME + timedelta(hours=5))
    )
    db_session.add(DevinSession(issue_id=other.id, status="finished", acu_used=99.0))
    db_session.commit()

    data = client.get("/metrics/summary").json()
    assert data["issues_opened"] == 1
    assert data["issues_merged"] == 1
    assert data["sessions_total"] == 1
    assert data["cost_acu"] == 2.0
    assert data["mttr_hours"] == 1.0

    rows = client.get("/metrics/sessions").json()
    assert len(rows) == 1
    assert rows[0]["acu_used"] == 2.0

    series = client.get("/metrics/timeseries").json()
    total_merged = sum(p["merged_count"] for p in series)
    assert total_merged == 1


def test_sessions_endpoint_empty_store(client):
    resp = client.get("/metrics/sessions")
    assert resp.status_code == 200
    assert resp.json() == []


def test_sessions_endpoint_running_session_with_no_pr(client, db_session):
    issue = _make_issue(db_session, 3001, github_issue_number=42, title="MD5 hardening")
    db_session.add(DevinSession(issue_id=issue.id, status="working", acu_used=1.2))
    db_session.commit()

    rows = client.get("/metrics/sessions").json()
    assert rows == [
        {"issue_number": 42, "title": "MD5 hardening", "status": "working", "acu_used": 1.2, "pr_url": None}
    ]


def test_sessions_endpoint_merged_pr_wins_over_stale_outcome(client, db_session):
    """merged=True on the PR state must win even if the session's own outcome
    hasn't been resolved yet -- GitHub's merge response is the authoritative
    signal (see app/poller.py), not the session's self-reported status."""
    issue = _make_issue(db_session, 3002, github_issue_number=43, title="slice.py XSS")
    session = DevinSession(issue_id=issue.id, status="working", acu_used=3.4)
    db_session.add(session)
    db_session.flush()
    db_session.add(
        SessionPrState(session_id=session.id, pr_url="https://github.com/x/y/pull/12", merged=True)
    )
    db_session.commit()

    rows = client.get("/metrics/sessions").json()
    assert rows[0]["status"] == "merged"
    assert rows[0]["pr_url"] == "https://github.com/x/y/pull/12"


def test_sessions_endpoint_escalated(client, db_session):
    issue = _make_issue(db_session, 3003, github_issue_number=44)
    db_session.add(
        DevinSession(
            issue_id=issue.id,
            status="working",
            escalated_at=utcnow(),
            escalated_reason="acu_ratio",
        )
    )
    db_session.commit()

    rows = client.get("/metrics/sessions").json()
    assert rows[0]["status"] == "escalated -- acu_ratio"


def test_sessions_endpoint_dismissed_false_positive(client, db_session):
    issue = _make_issue(db_session, 3004, github_issue_number=45)
    db_session.add(DevinSession(issue_id=issue.id, status="exit", outcome="false_positive"))
    db_session.commit()

    rows = client.get("/metrics/sessions").json()
    assert rows[0]["status"] == "dismissed -- false positive"


def test_sessions_endpoint_pr_open_shows_ci_outcome(client, db_session):
    issue = _make_issue(db_session, 3005, github_issue_number=46)
    session = DevinSession(issue_id=issue.id, status="working")
    db_session.add(session)
    db_session.flush()
    db_session.add(
        SessionPrState(
            session_id=session.id,
            pr_url="https://github.com/x/y/pull/13",
            ci_outcome="fail",
            merged=False,
        )
    )
    db_session.commit()

    rows = client.get("/metrics/sessions").json()
    assert rows[0]["status"] == "PR open -- CI fail"


@respx.mock
def test_devin_org_metrics_none_on_api_failure(client, monkeypatch):
    monkeypatch.setenv("DEVIN_API_KEY", "test-key")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-1")
    monkeypatch.setenv("DEVIN_PLAYBOOK_ID", "playbook-abc")
    get_settings.cache_clear()
    try:
        respx.get("https://api.devin.ai/v3/organizations/org-1/metrics/sessions").mock(
            return_value=httpx.Response(403, json={"detail": "Unauthorized"})
        )

        resp = client.get("/metrics/summary")

        assert resp.status_code == 200
        assert resp.json()["devin_org_metrics"] is None
    finally:
        get_settings.cache_clear()
