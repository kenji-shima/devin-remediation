from pydantic import BaseModel


class DevinOrgMetrics(BaseModel):
    """Devin's own org-level analytics (GET /v3/organizations/{org_id}/metrics/*).
    None if Devin credentials aren't configured or the call fails.

    Not rendered on the dashboard: this endpoint has no working filter that
    scopes it to just this system's sessions (confirmed live -- the `tags`
    filter on the sessions-list endpoint is documented but does not actually
    filter). Kept here, real and tested, for inspection via this API/at
    /docs -- see avg_acu_per_session on MetricsSummary for the trustworthy,
    self-scoped equivalent.
    """

    sessions_count: int
    prs_created_count: int
    prs_merged_count: int
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
    # cost_acu / sessions_total -- computed from our own sessions rather than
    # Devin's org-wide avg_acus_per_session, which can't be scoped to just
    # this system's activity (see devin_org_metrics docstring).
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
