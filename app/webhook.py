import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Issue, Transition, WebhookDelivery, utcnow
from app.security import verify_signature
from app.session_manager import create_session_for_issue

logger = logging.getLogger(__name__)

router = APIRouter()

# Time-bounded dedup window for same issue+action+label redelivered under a new
# delivery id. Intentionally NOT a permanent unique constraint on
# (github_issue_id, action, label_name): label -> unlabel -> re-label on the same
# issue is a legitimate, repeatable sequence (and Phase 2's re-trigger path) that
# must not be blocked forever.
DEDUP_WINDOW = timedelta(seconds=30)


def _parse_github_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    # Naive UTC to match storage (see app.models.utcnow).
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    # HMAC must be verified against the exact raw bytes GitHub sent -- never a
    # Pydantic body param, which would re-serialize the payload before hashing
    # and silently break verification.
    raw = await request.body()

    if not verify_signature(raw, x_hub_signature_256, settings.webhook_secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    if not x_github_delivery:
        raise HTTPException(status_code=400, detail="missing X-GitHub-Delivery header")

    payload = json.loads(raw)
    event_type = x_github_event or "unknown"
    action = payload.get("action")
    issue_payload = payload.get("issue") or {}
    github_issue_id = issue_payload.get("id")
    label_name = (payload.get("label") or {}).get("name")

    now = utcnow()

    is_time_duplicate = False
    if github_issue_id is not None and action is not None:
        cutoff = now - DEDUP_WINDOW
        existing = db.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.github_issue_id == github_issue_id,
                WebhookDelivery.action == action,
                WebhookDelivery.label_name == label_name,
                WebhookDelivery.received_at > cutoff,
            )
        ).first()
        is_time_duplicate = existing is not None

    delivery = WebhookDelivery(
        delivery_id=x_github_delivery,
        event_type=event_type,
        action=action,
        github_issue_id=github_issue_id,
        label_name=label_name,
        received_at=now,
    )
    db.add(delivery)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"status": "duplicate"}

    if is_time_duplicate:
        return {"status": "duplicate"}

    if event_type == "issues" and action == "labeled" and github_issue_id is not None:
        issue = db.execute(
            select(Issue).where(Issue.github_issue_id == github_issue_id)
        ).scalar_one_or_none()

        repo_payload = payload.get("repository") or {}
        owner_payload = repo_payload.get("owner") or {}
        github_created_at = _parse_github_datetime(issue_payload.get("created_at")) or now

        if issue is None:
            issue = Issue(
                github_issue_id=github_issue_id,
                github_issue_number=issue_payload.get("number", 0),
                repo_owner=owner_payload.get("login", ""),
                repo_name=repo_payload.get("name", ""),
                title=issue_payload.get("title", ""),
                body=issue_payload.get("body"),
                label=label_name,
                state=issue_payload.get("state", "open"),
                github_created_at=github_created_at,
            )
            db.add(issue)
            db.flush()
        else:
            issue.title = issue_payload.get("title", issue.title)
            issue.body = issue_payload.get("body", issue.body)
            issue.label = label_name
            issue.state = issue_payload.get("state", issue.state)

        has_created_transition = db.execute(
            select(Transition).where(
                Transition.issue_id == issue.id, Transition.type == "issue_created"
            )
        ).first()
        if has_created_transition is None:
            db.add(
                Transition(
                    issue_id=issue.id,
                    type="issue_created",
                    occurred_at=github_created_at,
                )
            )

        db.commit()

        if label_name is not None:
            if label_name == "devin-fix":
                # Human-legible on purpose -- this line is what the live demo
                # shows on screen as proof the webhook was received and the
                # session hand-off is starting, not just an HTTP 200.
                logger.info(
                    "webhook received: issue #%s labeled 'devin-fix' -> creating Devin session",
                    issue.github_issue_number,
                )
            background_tasks.add_task(create_session_for_issue, issue_id=issue.id, label=label_name)

    return {"status": "ok"}
