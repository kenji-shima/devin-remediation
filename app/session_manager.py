import logging

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.devin_client import create_session
from app.models import DevinSession, Issue, Transition, utcnow

logger = logging.getLogger(__name__)


def _build_prompt(github_issue_number: int, title: str, body: str | None) -> str:
    # Test-until-green and PR-closing-keyword instructions live in the
    # playbook (Phase 2/3) now, not here -- this only needs to state the
    # specific task. The issue number still appears here so the playbook's
    # generic "reference the issue using closing keywords" instruction has a
    # concrete number to pick up.
    body_text = body or "(no description provided)"
    return f"Resolve issue #{github_issue_number}: {title}\n\n{body_text}"


async def create_session_for_issue(*, issue_id: int, label: str) -> None:
    """Real, wired entry point for the devin-fix label.

    Opens its own fresh DB session -- must NOT reuse the webhook request's
    session, since this runs via BackgroundTasks after the response and the
    request-scoped session's lifecycle can't be depended on across that
    boundary. Looks up the Issue itself rather than taking a detached ORM
    object or a long parameter list.
    """
    if label != "devin-fix":
        return

    settings = get_settings()
    db = SessionLocal()
    try:
        issue = db.get(Issue, issue_id)
        if issue is None:
            logger.error("Issue id=%s not found -- cannot create Devin session", issue_id)
            return

        existing = db.execute(
            select(DevinSession).where(
                DevinSession.issue_id == issue_id,
                DevinSession.lifecycle_stage == "active",
            )
        ).scalars().first()
        if existing is not None:
            logger.info(
                "Issue #%s already has an active session (%s) -- skipping",
                issue.github_issue_number, existing.devin_session_id,
            )
            return

        active_sessions = db.execute(
            select(DevinSession).where(DevinSession.lifecycle_stage == "active")
        ).scalars().all()
        if len(active_sessions) >= settings.max_concurrent_sessions:
            logger.warning(
                "Concurrency cap (%s) reached -- skipping session creation for issue #%s",
                settings.max_concurrent_sessions, issue.github_issue_number,
            )
            return

        prompt = _build_prompt(issue.github_issue_number, issue.title, issue.body)
        tags = ["devin-remediation", f"issue-{issue.github_issue_number}"]

        async with httpx.AsyncClient(timeout=30) as client:
            response = await create_session(
                org_id=settings.devin_org_id,
                api_key=settings.devin_api_key,
                prompt=prompt,
                tags=tags,
                max_acu_limit=settings.acu_cap,
                create_as_user_id=settings.devin_user_id or None,
                playbook_id=settings.devin_playbook_id or None,
                client=client,
            )

        session = DevinSession(
            issue_id=issue_id,
            devin_session_id=response["session_id"],
            tag=tags[-1],
            status=response.get("status", "new"),
            acu_cap=settings.acu_cap,
            lifecycle_stage="active",
        )
        db.add(session)
        db.flush()

        db.add(
            Transition(
                issue_id=issue_id,
                session_id=session.id,
                type="session_started",
                occurred_at=utcnow(),
            )
        )
        db.commit()
        logger.info(
            "Created Devin session %s for issue #%s", session.devin_session_id, issue.github_issue_number
        )
    except Exception:
        logger.exception("Failed to create Devin session for issue id=%s", issue_id)
        db.rollback()
    finally:
        db.close()
