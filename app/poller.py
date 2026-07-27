import asyncio
import hashlib
import json
import logging
import random
from datetime import timedelta

import httpx
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.devin_client import get_session as devin_get_session
from app.devin_client import send_message as devin_send_message
from app.github_client import get_pull_request, list_check_runs, merge_pull_request, parse_pr_url
from app.models import DevinSession, SessionPrState, Transition, utcnow

logger = logging.getLogger(__name__)

MAX_POLL_ERRORS = 10
# Devin's own hard-stop states -- no amount of polling changes these.
HARD_STOP_STATUS_DETAILS = {
    "out_of_credits",
    "out_of_quota",
    "no_quota_allocation",
    "payment_declined",
    "usage_limit_exceeded",
    "org_usage_limit_exceeded",
    "total_session_limit_exceeded",
}
_NON_FAILING_CONCLUSIONS = {"success", "skipped", "neutral"}


class ApiError(Exception):
    """Raised after retries are exhausted (or on a non-retryable response)."""


async def call_with_retry(func, *args, attempts: int = 3, **kwargs):
    """Retry a single async call on transient failures only (timeouts,
    connection errors, 5xx, 429 honoring Retry-After). Never retries 4xx.
    Scoped to one HTTP call, not a session or a whole tick -- retrying a
    side-effecting call like send_message at a broader scope risks
    re-invoking it after a lost-response success.
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return await func(*args, **kwargs)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 429 or status >= 500:
                last_exc = exc
                if attempt < attempts - 1:
                    retry_after = exc.response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else (2**attempt) + random.random()
                    await asyncio.sleep(delay)
                    continue
            raise ApiError(str(exc)) from exc
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < attempts - 1:
                await asyncio.sleep((2**attempt) + random.random())
                continue
            raise ApiError(str(exc)) from exc
    raise ApiError(str(last_exc))


def _failure_signature(failing_check_runs: list[dict]) -> str:
    pairs = sorted((cr.get("name", ""), cr.get("conclusion", "")) for cr in failing_check_runs)
    return hashlib.sha256(str(pairs).encode()).hexdigest()


def compute_ci_outcome(check_runs: list[dict]) -> tuple[str, str | None]:
    """Returns (outcome, failure_signature). outcome in {no_ci, pending, pass, fail}.

    An empty check-runs list is always "no_ci", never "pass" -- treating an
    empty list as green would auto-merge a PR before GitHub has even
    registered a check run. Without an explicit "this repo has no CI" config
    flag (not built), a chronically-empty list simply blocks auto-merge
    forever and the session eventually escalates via max_age -- a safe
    default, not a bug.
    """
    if not check_runs:
        return "no_ci", None
    if any(cr.get("status") != "completed" for cr in check_runs):
        return "pending", None
    failing = [cr for cr in check_runs if cr.get("conclusion") not in _NON_FAILING_CONCLUSIONS]
    if failing:
        return "fail", _failure_signature(failing)
    return "pass", None


def _build_review_message(check_runs: list[dict]) -> str:
    failing = [cr for cr in check_runs if cr.get("conclusion") not in _NON_FAILING_CONCLUSIONS]
    lines = ["CI is failing on the latest commit. Failing checks:"]
    for cr in failing:
        lines.append(f"- {cr.get('name')}: {cr.get('conclusion')}")
    lines.append("Please investigate and push a fix.")
    return "\n".join(lines)


def _get_pr_state(db, session_id: int, pr_url: str) -> SessionPrState:
    """Plain lookup -- callers must already know the row exists (created via
    _get_or_create_pr_state earlier in the same poll_one_session call)."""
    return (
        db.execute(
            select(SessionPrState).where(
                SessionPrState.session_id == session_id, SessionPrState.pr_url == pr_url
            )
        )
        .scalars()
        .first()
    )


def _get_or_create_pr_state(db, session_id: int, issue_id: int, pr_url: str) -> SessionPrState:
    existing = _get_pr_state(db, session_id, pr_url)
    if existing is not None:
        return existing
    state = SessionPrState(session_id=session_id, pr_url=pr_url)
    db.add(state)
    db.add(
        Transition(
            session_id=session_id, issue_id=issue_id, type="pr_opened", pr_url=pr_url, occurred_at=utcnow()
        )
    )
    db.commit()
    db.refresh(state)
    return state


def _finalize_merge(db, session_id: int, issue_id: int, pr_url: str) -> None:
    pr_state = (
        db.execute(
            select(SessionPrState).where(
                SessionPrState.session_id == session_id, SessionPrState.pr_url == pr_url
            )
        )
        .scalars()
        .first()
    )
    if pr_state.merged:
        return
    pr_state.merged = True

    already_recorded = db.execute(
        select(Transition).where(Transition.session_id == session_id, Transition.type == "merged")
    ).first()
    if already_recorded is None:
        db.add(
            Transition(
                session_id=session_id,
                issue_id=issue_id,
                type="merged",
                pr_url=pr_url,
                outcome="merged",
                occurred_at=utcnow(),
            )
        )

    session = db.get(DevinSession, session_id)
    session.goal_achieved = True
    session.outcome = "fixed"
    session.lifecycle_stage = "done"
    db.commit()


def _maybe_resolve_terminal(db, session: DevinSession, devin_resp: dict, any_pr_unresolved: bool) -> None:
    """Resolve goal_achieved once Devin has reported a definitive verdict and
    no PR is left unresolved.

    Must run AFTER all PR/CI handling for this tick and be gated on no PR
    being left unresolved -- otherwise a same-tick "status flips to exit
    right as CI turns green" race could mark the session failed and drop it
    from polling before the merge ever happens. `exit` alone never implies
    success.

    Resolution isn't gated on raw status == exit/error: a triage-only run
    (playbook Phase 2 Option A -- false positive, no code change, just a
    comment) can sit in status="running"/status_detail="waiting_for_user"
    indefinitely with no PR ever coming, even though structured_output
    already carries a final outcome. Treating a populated structured_output
    as equally terminal avoids that session polling forever until a
    misleading max_age escalation.
    """
    if any_pr_unresolved or session.goal_achieved is not None:
        return
    status = devin_resp.get("status")
    structured_output = devin_resp.get("structured_output")
    if status not in ("exit", "error") and not structured_output:
        return

    structured_output = structured_output or {}
    outcome = structured_output.get("outcome")
    session.goal_achieved = outcome == "fixed"
    session.outcome = outcome
    session.lifecycle_stage = "done"
    db.add(
        Transition(
            session_id=session.id,
            issue_id=session.issue_id,
            type="session_resolved",
            outcome=outcome or status,
            occurred_at=utcnow(),
        )
    )


def _escalate(session: DevinSession, reason: str) -> None:
    session.lifecycle_stage = "escalated"
    session.escalated_at = utcnow()
    session.escalated_reason = reason
    logger.warning("Session %s escalated: %s", session.devin_session_id, reason)


def _maybe_escalate(db, session: DevinSession, settings: Settings) -> None:
    if session.escalated_at is not None or session.lifecycle_stage != "active":
        return

    if session.status_detail in HARD_STOP_STATUS_DETAILS:
        _escalate(session, "quota_exhausted")
        return

    if session.acu_cap and session.acu_used and session.acu_used >= session.acu_cap * settings.escalation_acu_ratio:
        _escalate(session, "acu_ratio")
        return

    age = utcnow() - session.created_at
    if age > timedelta(hours=settings.session_max_age_hours):
        _escalate(session, "max_age")
        return

    if not settings.auto_merge:
        unmerged_green = (
            db.execute(
                select(SessionPrState).where(
                    SessionPrState.session_id == session.id,
                    SessionPrState.ci_outcome == "pass",
                    SessionPrState.merged.is_(False),
                )
            )
            .scalars()
            .first()
        )
        if unmerged_green is not None:
            _escalate(session, "awaiting_human_merge")


async def poll_one_session(session_id: int, settings: Settings, http_client: httpx.AsyncClient) -> None:
    with SessionLocal() as db:
        session = db.get(DevinSession, session_id)
        if session is None or session.lifecycle_stage != "active":
            return
        devin_session_id = session.devin_session_id
        issue_id = session.issue_id

    try:
        devin_resp = await call_with_retry(
            devin_get_session,
            org_id=settings.devin_org_id,
            api_key=settings.devin_api_key,
            devin_session_id=devin_session_id,
            client=http_client,
        )
    except ApiError:
        logger.exception("Devin GET failed for session id=%s", session_id)
        with SessionLocal() as db:
            session = db.get(DevinSession, session_id)
            session.consecutive_poll_errors += 1
            session.last_polled_at = utcnow()
            if session.consecutive_poll_errors >= MAX_POLL_ERRORS:
                _escalate(session, "poll_failures")
            db.commit()
        return

    with SessionLocal() as db:
        session = db.get(DevinSession, session_id)
        session.status = devin_resp.get("status", session.status)
        session.status_detail = devin_resp.get("status_detail")
        session.acu_used = devin_resp.get("acus_consumed", session.acu_used)
        session.last_polled_at = utcnow()
        session.consecutive_poll_errors = 0
        db.commit()

    any_pr_unresolved = False
    session_resolved = False

    for pr in devin_resp.get("pull_requests") or []:
        pr_url = pr.get("pr_url")
        if not pr_url:
            continue

        parsed = parse_pr_url(pr_url)
        if parsed is None:
            logger.warning("Could not parse PR URL %r for session id=%s -- skipping", pr_url, session_id)
            any_pr_unresolved = True
            continue
        owner, repo, number = parsed

        with SessionLocal() as db:
            pr_state = _get_or_create_pr_state(db, session_id, issue_id, pr_url)
            if pr_state.merged or pr_state.pr_resolved_without_merge:
                continue
            stored_head_sha = pr_state.head_sha
            stored_ci_outcome = pr_state.ci_outcome
            stored_failure_sig = pr_state.failure_signature
            stored_last_msg_sig = pr_state.last_message_signature_sent

        try:
            pr_detail = await call_with_retry(
                get_pull_request,
                owner=owner,
                repo=repo,
                number=number,
                token=settings.github_token,
                client=http_client,
            )
        except ApiError:
            logger.exception("GitHub GET pull failed for %s", pr_url)
            any_pr_unresolved = True
            continue

        if pr_detail.get("merged"):
            with SessionLocal() as db:
                _finalize_merge(db, session_id, issue_id, pr_url)
            session_resolved = True
            break

        if pr_detail.get("state") == "closed":
            with SessionLocal() as db:
                pr_state = _get_pr_state(db, session_id, pr_url)
                pr_state.pr_resolved_without_merge = True
                db.commit()
            continue

        head_sha = (pr_detail.get("head") or {}).get("sha")

        # Skip the expensive check-runs fetch when nothing could have changed:
        # same sha, and the last known outcome for it was already terminal
        # (pass already handled above via merge; fail we've already messaged).
        if head_sha == stored_head_sha and stored_ci_outcome == "fail" and stored_failure_sig == stored_last_msg_sig:
            any_pr_unresolved = True
            continue

        try:
            check_runs = await call_with_retry(
                list_check_runs,
                owner=owner,
                repo=repo,
                sha=head_sha,
                token=settings.github_token,
                client=http_client,
            )
        except ApiError:
            logger.exception("GitHub check-runs fetch failed for %s@%s", pr_url, head_sha)
            any_pr_unresolved = True
            continue

        outcome, failure_sig = compute_ci_outcome(check_runs)
        is_new_state = (
            outcome != stored_ci_outcome
            or head_sha != stored_head_sha
            or (outcome == "fail" and failure_sig != stored_failure_sig)
        )
        if is_new_state:
            with SessionLocal() as db:
                db.add(
                    Transition(
                        session_id=session_id,
                        issue_id=issue_id,
                        type="ci_result",
                        outcome=outcome,
                        pr_url=pr_url,
                        occurred_at=utcnow(),
                        transition_metadata=json.dumps({"head_sha": head_sha, "failure_signature": failure_sig}),
                    )
                )
                pr_state = _get_pr_state(db, session_id, pr_url)
                pr_state.head_sha = head_sha
                pr_state.ci_outcome = outcome
                pr_state.failure_signature = failure_sig
                db.commit()

        if outcome == "fail":
            any_pr_unresolved = True
            if failure_sig != stored_last_msg_sig:
                try:
                    await call_with_retry(
                        devin_send_message,
                        org_id=settings.devin_org_id,
                        api_key=settings.devin_api_key,
                        devin_session_id=devin_session_id,
                        message=_build_review_message(check_runs),
                        client=http_client,
                    )
                    with SessionLocal() as db:
                        pr_state = _get_pr_state(db, session_id, pr_url)
                        pr_state.last_message_signature_sent = failure_sig
                        db.commit()
                except ApiError:
                    logger.exception("Failed to send review-fix message for %s", pr_url)
        elif outcome == "pass":
            if settings.auto_merge:
                try:
                    await call_with_retry(
                        merge_pull_request,
                        owner=owner,
                        repo=repo,
                        number=number,
                        token=settings.github_token,
                        client=http_client,
                    )
                    with SessionLocal() as db:
                        _finalize_merge(db, session_id, issue_id, pr_url)
                    session_resolved = True
                    break
                except ApiError:
                    logger.exception("Merge failed for %s", pr_url)
                    any_pr_unresolved = True
            else:
                any_pr_unresolved = True
        else:  # no_ci or pending
            any_pr_unresolved = True

    if session_resolved:
        return

    with SessionLocal() as db:
        session = db.get(DevinSession, session_id)
        if session.lifecycle_stage != "active":
            return
        _maybe_resolve_terminal(db, session, devin_resp, any_pr_unresolved)
        _maybe_escalate(db, session, settings)
        db.commit()


async def run_one_tick(settings: Settings, http_client: httpx.AsyncClient) -> None:
    with SessionLocal() as db:
        active_ids = [
            s.id
            for s in db.execute(select(DevinSession).where(DevinSession.lifecycle_stage == "active")).scalars().all()
        ]

    semaphore = asyncio.Semaphore(settings.max_concurrent_http)

    async def guarded(session_id: int) -> None:
        async with semaphore:
            try:
                await poll_one_session(session_id, settings, http_client)
            except Exception:
                logger.exception("poll_one_session crashed for id=%s", session_id)

    await asyncio.gather(*(guarded(sid) for sid in active_ids))


async def poll_loop(stop_event: asyncio.Event) -> None:
    """Runs forever until stop_event is set. Ticks never overlap: the next
    tick's sleep is (interval - elapsed), and a tick isn't considered started
    until the previous one's gather over all active sessions has fully
    returned -- this prevents two ticks from racing on the same
    SessionPrState row.
    """
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as http_client:
        while not stop_event.is_set():
            start = utcnow()
            try:
                await run_one_tick(settings, http_client)
            except Exception:
                logger.exception("Poller tick crashed, continuing")
            elapsed = (utcnow() - start).total_seconds()
            remaining = max(0.0, settings.poll_interval_seconds - elapsed)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass
