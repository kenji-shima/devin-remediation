import logging
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.devin_client import get_org_pr_metrics, get_org_session_metrics
from app.models import DevinSession, Issue, Transition, utcnow
from app.schemas import DevinOrgMetrics, MetricsSummary, SessionRow, TimeseriesPoint

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/metrics", tags=["metrics"])


def target_repo_issue_ids(db: Session, settings: Settings) -> set[int] | None:
    """Issue ids belonging to the configured target repo (GITHUB_REPO_OWNER/
    GITHUB_REPO_NAME) -- every metric below is scoped to this set so a
    calibration/test repo used during development can never leak into the
    numbers shown for the real target. None means no repo is configured
    (falls back to unscoped), never an empty-set silent filter that would
    make every metric look empty.
    """
    if not (settings.github_repo_owner and settings.github_repo_name):
        return None
    return set(
        db.execute(
            select(Issue.id).where(
                Issue.repo_owner == settings.github_repo_owner,
                Issue.repo_name == settings.github_repo_name,
            )
        ).scalars()
    )


def _transitions_of_type(db: Session, type_: str, issue_ids: set[int] | None = None) -> list[Transition]:
    query = select(Transition).where(Transition.type == type_)
    if issue_ids is not None:
        query = query.where(Transition.issue_id.in_(issue_ids))
    return list(db.execute(query).scalars())


def _sessions_query(issue_ids: set[int] | None):
    query = select(DevinSession)
    if issue_ids is not None:
        query = query.where(DevinSession.issue_id.in_(issue_ids))
    return query


def compute_mttr(db: Session, issue_ids: set[int] | None = None) -> float | None:
    """Mean(merged_at - issue_created_at) in hours, over issues with both events."""
    created = {t.issue_id: t.occurred_at for t in _transitions_of_type(db, "issue_created", issue_ids)}
    merged = {t.issue_id: t.occurred_at for t in _transitions_of_type(db, "merged", issue_ids)}
    durations = [
        (merged[issue_id] - created[issue_id]).total_seconds()
        for issue_id in created
        if issue_id in merged
    ]
    if not durations:
        return None
    return (sum(durations) / len(durations)) / 3600.0


def compute_merge_rate(db: Session, issue_ids: set[int] | None = None) -> float | None:
    opened = len({t.issue_id for t in _transitions_of_type(db, "issue_created", issue_ids)})
    if opened == 0:
        return None
    merged = len({t.issue_id for t in _transitions_of_type(db, "merged", issue_ids)})
    return merged / opened


def compute_success_rate(db: Session, issue_ids: set[int] | None = None) -> float | None:
    """fixed / (fixed + blocked) -- sessions where a fix was actually
    attempted. Excludes false_positive dismissals from the denominator
    entirely: a correctly-diagnosed false positive is a good outcome, not a
    remediation failure, and blending it in would make accurate triage look
    like a worse success rate than never triaging ambiguous findings at all.
    """
    sessions = list(db.execute(_sessions_query(issue_ids)).scalars())
    remediation_attempts = [s for s in sessions if s.outcome in ("fixed", "blocked")]
    if not remediation_attempts:
        return None
    achieved = sum(1 for s in remediation_attempts if s.outcome == "fixed")
    return achieved / len(remediation_attempts)


def compute_triage_dismissals(db: Session, issue_ids: set[int] | None = None) -> int:
    """Count of sessions Devin correctly diagnosed as false positives --
    reported as its own count, never blended into success_rate."""
    query = select(DevinSession).where(DevinSession.outcome == "false_positive")
    if issue_ids is not None:
        query = query.where(DevinSession.issue_id.in_(issue_ids))
    return len(list(db.execute(query).scalars()))


def compute_throughput(db: Session, window_days: int = 7, issue_ids: set[int] | None = None) -> float | None:
    opened = _transitions_of_type(db, "issue_created", issue_ids)
    if not opened:
        return None
    cutoff = utcnow() - timedelta(days=window_days)
    recent_merges = [t for t in _transitions_of_type(db, "merged", issue_ids) if t.occurred_at > cutoff]
    return len(recent_merges) / window_days


def compute_cost(db: Session, issue_ids: set[int] | None = None) -> float | None:
    sessions = list(db.execute(_sessions_query(issue_ids)).scalars())
    if not sessions:
        return None
    return sum(s.acu_used or 0.0 for s in sessions)


def compute_cost_per_fix(db: Session, issue_ids: set[int] | None = None) -> float | None:
    """Cost per merged fix -- the one framing that turns a raw ACU number
    into a business number a leader can compare against engineer time. None
    until at least one issue has merged, since dividing by zero outcomes
    would be a fabricated number, not a conservative default.
    """
    merged = len({t.issue_id for t in _transitions_of_type(db, "merged", issue_ids)})
    if merged == 0:
        return None
    cost = compute_cost(db, issue_ids)
    return (cost or 0.0) / merged


def compute_avg_acu_per_session(db: Session, issue_ids: set[int] | None = None) -> float | None:
    """Our own equivalent of Devin's avg_acus_per_session, computed from
    sessions this orchestrator created -- correctly scoped by construction,
    unlike the org-wide endpoint (see DevinOrgMetrics docstring).
    """
    sessions = list(db.execute(_sessions_query(issue_ids)).scalars())
    if not sessions:
        return None
    return sum(s.acu_used or 0.0 for s in sessions) / len(sessions)


def compute_timeseries(db: Session, days: int = 30, issue_ids: set[int] | None = None) -> list[dict]:
    merges = _transitions_of_type(db, "merged", issue_ids)
    if not merges:
        return []

    counts: dict[str, int] = {}
    for t in merges:
        key = t.occurred_at.date().isoformat()
        counts[key] = counts.get(key, 0) + 1

    today = utcnow().date()
    series = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        key = day.isoformat()
        series.append({"date": key, "merged_count": counts.get(key, 0)})
    return series


def _display_status(session: DevinSession, pr_state) -> str:
    """A merged PR is checked first regardless of the session's own outcome
    fields -- SessionPrState.merged is set directly from GitHub's merge
    response (see app/poller.py), the most authoritative signal available,
    and should win even if the session hasn't formally resolved yet.
    """
    if pr_state is not None and pr_state.merged:
        return "merged"
    if session.escalated_at is not None:
        return f"escalated -- {session.escalated_reason}"
    if session.outcome == "false_positive":
        return "dismissed -- false positive"
    if session.outcome == "blocked":
        return "blocked"
    if pr_state is not None:
        return f"PR open -- CI {pr_state.ci_outcome or 'no_ci'}"
    return session.status or "running"


def compute_session_rows(db: Session, issue_ids: set[int] | None = None) -> list[dict]:
    """One row per issue that has had at least one Devin session -- the
    per-task status list, distinct from the aggregate tiles above.
    """
    query = select(DevinSession.issue_id).distinct()
    if issue_ids is not None:
        query = query.where(DevinSession.issue_id.in_(issue_ids))
    issue_ids_with_sessions = set(db.execute(query).scalars())
    if not issue_ids_with_sessions:
        return []

    issues = list(
        db.execute(select(Issue).where(Issue.id.in_(issue_ids_with_sessions))).scalars()
    )
    rows = []
    for issue in issues:
        session = max(issue.sessions, key=lambda s: s.id)
        pr_state = max(session.pr_states, key=lambda p: p.updated_at, default=None)
        rows.append(
            {
                "issue_number": issue.github_issue_number,
                "title": issue.title,
                "status": _display_status(session, pr_state),
                "acu_used": session.acu_used,
                "pr_url": pr_state.pr_url if pr_state else None,
            }
        )
    rows.sort(key=lambda r: r["issue_number"])
    return rows


async def fetch_devin_org_metrics(settings: Settings, window_days: int = 30) -> DevinOrgMetrics | None:
    """Devin's own org-level analytics, scoped to our playbook and joined
    alongside (never replacing) our GitHub-derived business metrics. Returns
    None -- never raises -- if credentials or a playbook aren't configured,
    or the call fails, so a Devin API hiccup never breaks the dashboard.

    Requires devin_playbook_id specifically (not just the API key/org id):
    without it there is no verified-working way to scope this to just our
    own sessions, and showing unscoped org-wide numbers would silently pick
    up unrelated activity (see DevinOrgMetrics docstring).
    """
    if not (settings.devin_api_key and settings.devin_org_id and settings.devin_playbook_id):
        return None

    now = int(datetime.now(timezone.utc).timestamp())
    window_start = now - window_days * 24 * 3600
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            sessions = await get_org_session_metrics(
                org_id=settings.devin_org_id,
                api_key=settings.devin_api_key,
                time_after=window_start,
                time_before=now,
                playbook_id=settings.devin_playbook_id,
                client=client,
            )
            prs = await get_org_pr_metrics(
                org_id=settings.devin_org_id,
                api_key=settings.devin_api_key,
                time_after=window_start,
                time_before=now,
                playbook_id=settings.devin_playbook_id,
                client=client,
            )
    except httpx.HTTPError:
        logger.exception("Failed to fetch Devin org metrics")
        return None

    return DevinOrgMetrics(
        sessions_count=sessions.get("sessions_created_count", 0),
        prs_created_count=prs.get("prs_created_count", 0),
        prs_merged_count=prs.get("prs_merged_count", 0),
        prs_closed_count=prs.get("prs_closed_count", 0),
        avg_acus_per_session=sessions.get("avg_acus_per_session", 0.0),
    )


@router.get("/summary", response_model=MetricsSummary)
async def get_summary(db: Session = Depends(get_db)) -> MetricsSummary:
    settings = get_settings()
    issue_ids = target_repo_issue_ids(db, settings)
    opened = len({t.issue_id for t in _transitions_of_type(db, "issue_created", issue_ids)})
    merged = len({t.issue_id for t in _transitions_of_type(db, "merged", issue_ids)})
    sessions_total = len(list(db.execute(_sessions_query(issue_ids)).scalars()))

    return MetricsSummary(
        mttr_hours=compute_mttr(db, issue_ids),
        merge_rate=compute_merge_rate(db, issue_ids),
        success_rate=compute_success_rate(db, issue_ids),
        throughput_per_day=compute_throughput(db, issue_ids=issue_ids),
        cost_acu=compute_cost(db, issue_ids),
        cost_per_fix_acu=compute_cost_per_fix(db, issue_ids),
        avg_acu_per_session=compute_avg_acu_per_session(db, issue_ids),
        issues_opened=opened,
        issues_merged=merged,
        sessions_total=sessions_total,
        triage_dismissals=compute_triage_dismissals(db, issue_ids),
        devin_org_metrics=await fetch_devin_org_metrics(settings),
    )


@router.get("/timeseries", response_model=list[TimeseriesPoint])
def get_timeseries(db: Session = Depends(get_db)) -> list[dict]:
    settings = get_settings()
    return compute_timeseries(db, issue_ids=target_repo_issue_ids(db, settings))


@router.get("/sessions", response_model=list[SessionRow])
def get_sessions(db: Session = Depends(get_db)) -> list[dict]:
    settings = get_settings()
    return compute_session_rows(db, issue_ids=target_repo_issue_ids(db, settings))
