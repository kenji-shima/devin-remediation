from sqlalchemy import select

import app.session_manager as session_manager
from app.config import get_settings
from app.models import DevinSession, Issue, Transition, utcnow


def _make_issue(db_session, github_issue_id: int, github_issue_number: int = 1) -> Issue:
    issue = Issue(
        github_issue_id=github_issue_id,
        github_issue_number=github_issue_number,
        repo_owner="kenji-shima",
        repo_name="devin-remediation-webhook-test",
        title="requests==2.27.1 has known CVEs",
        body="fix it",
        label="devin-fix",
        state="open",
        github_created_at=utcnow(),
    )
    db_session.add(issue)
    db_session.commit()
    return issue


async def test_create_session_for_issue_creates_session_and_transition(
    patched_session_locals, db_session, monkeypatch
):
    issue = _make_issue(db_session, 3001)

    async def fake_create_session(**kwargs):
        return {"session_id": "devin-xyz", "status": "new"}

    monkeypatch.setattr(session_manager, "create_session", fake_create_session)

    await session_manager.create_session_for_issue(issue_id=issue.id, label="devin-fix")

    session = (
        db_session.execute(select(DevinSession).where(DevinSession.issue_id == issue.id)).scalars().first()
    )
    assert session is not None
    assert session.devin_session_id == "devin-xyz"
    assert session.lifecycle_stage == "active"
    assert session.status == "new"

    transition = (
        db_session.execute(
            select(Transition).where(Transition.session_id == session.id, Transition.type == "session_started")
        )
        .scalars()
        .first()
    )
    assert transition is not None


async def test_create_session_for_issue_ignores_non_devin_fix_label(patched_session_locals, db_session):
    issue = _make_issue(db_session, 3002)

    await session_manager.create_session_for_issue(issue_id=issue.id, label="needs-triage")

    session = (
        db_session.execute(select(DevinSession).where(DevinSession.issue_id == issue.id)).scalars().first()
    )
    assert session is None


async def test_create_session_for_issue_is_idempotent(patched_session_locals, db_session, monkeypatch):
    issue = _make_issue(db_session, 3003)
    call_count = {"n": 0}

    async def fake_create_session(**kwargs):
        call_count["n"] += 1
        return {"session_id": f"devin-{call_count['n']}", "status": "new"}

    monkeypatch.setattr(session_manager, "create_session", fake_create_session)

    await session_manager.create_session_for_issue(issue_id=issue.id, label="devin-fix")
    await session_manager.create_session_for_issue(issue_id=issue.id, label="devin-fix")

    assert call_count["n"] == 1
    sessions = db_session.execute(select(DevinSession).where(DevinSession.issue_id == issue.id)).scalars().all()
    assert len(sessions) == 1


async def test_create_session_for_issue_passes_playbook_id_when_configured(
    patched_session_locals, db_session, monkeypatch
):
    monkeypatch.setenv("DEVIN_PLAYBOOK_ID", "playbook-abc")
    get_settings.cache_clear()
    try:
        issue = _make_issue(db_session, 3006)
        captured = {}

        async def fake_create_session(**kwargs):
            captured.update(kwargs)
            return {"session_id": "devin-xyz", "status": "new"}

        monkeypatch.setattr(session_manager, "create_session", fake_create_session)

        await session_manager.create_session_for_issue(issue_id=issue.id, label="devin-fix")

        assert captured["playbook_id"] == "playbook-abc"
    finally:
        get_settings.cache_clear()


async def test_create_session_for_issue_respects_concurrency_cap(patched_session_locals, db_session, monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_SESSIONS", "1")
    get_settings.cache_clear()
    try:
        issue1 = _make_issue(db_session, 3004, github_issue_number=4)
        issue2 = _make_issue(db_session, 3005, github_issue_number=5)
        call_count = {"n": 0}

        async def fake_create_session(**kwargs):
            call_count["n"] += 1
            return {"session_id": f"devin-{call_count['n']}", "status": "new"}

        monkeypatch.setattr(session_manager, "create_session", fake_create_session)

        await session_manager.create_session_for_issue(issue_id=issue1.id, label="devin-fix")
        await session_manager.create_session_for_issue(issue_id=issue2.id, label="devin-fix")

        assert call_count["n"] == 1
        sessions = db_session.execute(select(DevinSession)).scalars().all()
        assert len(sessions) == 1
    finally:
        get_settings.cache_clear()
