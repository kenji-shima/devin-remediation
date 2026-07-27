from pydantic import BaseModel


class DevinOrgMetrics(BaseModel):
    """Devin's own org-level analytics (GET /v3/organizations/{org_id}/metrics/sessions
    and .../metrics/prs), scoped by playbook_id -- verified live to actually
    filter results, unlike the `tags` filter on the sessions-list endpoint
    (documented but does not filter at all) or the /metrics/usage endpoint
    (no filter parameters of any kind). None if Devin credentials or a
    playbook aren't configured, or the call fails.

    A secondary cross-check alongside the primary local metrics on
    MetricsSummary, not a replacement -- Devin's API has no concept of a
    GitHub issue's created_at, so it can never compute MTTR or the
    false-positive/triage-dismissal distinction our own metrics do.
    """

    sessions_count: int
    prs_created_count: int
    prs_merged_count: int
    prs_closed_count: int
    avg_acus_per_session: float


class MetricsSummary(BaseModel):
    mttr_hours: float | None
    merge_rate: float | None
    success_rate: float | None
    throughput_per_day: float | None
    cost_acu: float | None
    # cost_acu / issues_merged -- None until at least one issue has merged,
    # since a cost-per-outcome number is meaningless with no outcomes yet.
    cost_per_fix_acu: float | None
    # cost_acu / sessions_total -- our own primary source for this number.
    # devin_org_metrics.avg_acus_per_session is Devin's independent
    # cross-check on the same underlying activity, not the source of truth.
    avg_acu_per_session: float | None
    issues_opened: int
    issues_merged: int
    sessions_total: int
    # Sessions Devin correctly diagnosed as false positives -- reported on
    # its own, never blended into success_rate (see compute_success_rate).
    triage_dismissals: int
    devin_org_metrics: DevinOrgMetrics | None


class TimeseriesPoint(BaseModel):
    date: str
    merged_count: int


class SessionRow(BaseModel):
    """One row per issue that has had at least one Devin session -- the
    per-task status view the assignment brief asks for directly ("status of
    active and completed tasks"), which the aggregate metrics above can't
    provide on their own.
    """

    issue_number: int
    title: str
    status: str
    acu_used: float | None
    pr_url: str | None
