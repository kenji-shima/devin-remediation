"""Seed the datastore with representative demo data -- lets a reviewer without
Devin/GitHub credentials run `docker compose up` and see a populated dashboard
(real MTTR/merge-rate/success-rate/throughput/cost numbers, computed by the
real app/metrics.py queries over these rows) instead of an empty one.

This does NOT simulate the live webhook -> session -> merge loop itself (no
HTTP calls, no fake PR polling) -- see README's Known limitations. It only
seeds the datastore so the *observability* half of the system is inspectable
without live credentials.

Run inside the container: `docker compose exec orchestrator python -m app.seed_demo`
"""

from datetime import timedelta

from app.config import get_settings
from app.db import SessionLocal
from app.models import DevinSession, Issue, SessionPrState, Transition, utcnow

# Mirrors the shape of the real findings this system remediated (dependency,
# bandit, semgrep, gitleaks) -- representative, not a copy of live issue text.
DEMO_ISSUES = [
    {
        "number": 101,
        "title": "shell-quote <=1.8.4 has a high-severity DoS vulnerability (GHSA-395f-4hp3-45gv)",
        "label": "devin-fix",
        "age_days": 25,
        "mttr_hours": 1.2,
        "outcome": "fixed",
        "acu_used": 0.8,
        "pr_url": "https://github.com/kenji-shima/superset/pull/3",
    },
    {
        "number": 102,
        "title": "open_sql_lab_with_context.py builds a SQL Lab pre-fill query via unescaped string interpolation",
        "label": "devin-fix",
        "age_days": 20,
        "mttr_hours": None,
        "outcome": "false_positive",
        "acu_used": 0.4,
        "pr_url": None,
    },
    {
        "number": 103,
        "title": 'slice.py builds a chart link via Markup(f"...") flagged as potential unescaped XSS',
        "label": "devin-fix",
        "age_days": 15,
        "mttr_hours": None,
        "outcome": "false_positive",
        "acu_used": 0.3,
        "pr_url": None,
    },
    {
        "number": 104,
        "title": 'sql_lab.py\'s pop_tab_link interpolates an id into Markup(f"...") with no escaping, unlike its sibling link properties',
        "label": "devin-fix",
        "age_days": 4,
        "mttr_hours": 0.7,
        "outcome": "fixed",
        "acu_used": 0.6,
        "pr_url": "https://github.com/kenji-shima/superset/pull/8",
    },
    {
        "number": 105,
        "title": "mcp-server.mdx contains a hardcoded Bearer token in a curl example (GHSA-style secret scan hit)",
        "label": "devin-fix",
        "age_days": 2,
        "mttr_hours": None,
        "outcome": "false_positive",
        "acu_used": 0.2,
        "pr_url": None,
    },
    {
        "number": 106,
        "title": "raise_for_access in security/manager.py uses assert on a datasource derived from query_context/viz",
        "label": "devin-fix",
        "age_days": 1,
        "mttr_hours": None,
        "outcome": "false_positive",
        "acu_used": 0.3,
        "pr_url": None,
    },
]


def seed() -> None:
    settings = get_settings()
    repo_owner = settings.github_repo_owner or "demo-owner"
    repo_name = settings.github_repo_name or "demo-superset"

    with SessionLocal() as db:
        if db.query(Issue).first() is not None:
            print("Datastore already has issues -- skipping seed (demo data is meant for an empty store).")
            return

        now = utcnow()
        for spec in DEMO_ISSUES:
            created_at = now - timedelta(days=spec["age_days"])

            issue = Issue(
                github_issue_id=100000 + spec["number"],
                github_issue_number=spec["number"],
                repo_owner=repo_owner,
                repo_name=repo_name,
                title=spec["title"],
                label=spec["label"],
                state="closed" if spec["outcome"] == "fixed" else "open",
                github_created_at=created_at,
            )
            db.add(issue)
            db.flush()  # assign issue.id

            db.add(
                Transition(
                    issue_id=issue.id,
                    type="issue_created",
                    occurred_at=created_at,
                )
            )

            session_started_at = created_at + timedelta(minutes=3)
            session = DevinSession(
                issue_id=issue.id,
                devin_session_id=f"demo-session-{spec['number']}",
                tag=f"devin-remediation,issue-{spec['number']}",
                status="exit",
                status_detail="finished",
                goal_achieved=(spec["outcome"] == "fixed"),
                outcome=spec["outcome"],
                acu_used=spec["acu_used"],
                acu_cap=10.0,
                lifecycle_stage="done",
                created_at=session_started_at,
            )
            db.add(session)
            db.flush()

            db.add(
                Transition(
                    issue_id=issue.id,
                    session_id=session.id,
                    type="session_started",
                    occurred_at=session_started_at,
                )
            )

            if spec["outcome"] == "fixed":
                merged_at = created_at + timedelta(hours=spec["mttr_hours"])
                pr_opened_at = merged_at - timedelta(minutes=15)

                db.add(
                    Transition(
                        issue_id=issue.id,
                        session_id=session.id,
                        type="pr_opened",
                        occurred_at=pr_opened_at,
                        pr_url=spec["pr_url"],
                    )
                )
                db.add(
                    Transition(
                        issue_id=issue.id,
                        session_id=session.id,
                        type="merged",
                        occurred_at=merged_at,
                        pr_url=spec["pr_url"],
                    )
                )
                db.add(
                    SessionPrState(
                        session_id=session.id,
                        pr_url=spec["pr_url"],
                        head_sha="0" * 40,
                        ci_outcome="pass",
                        merged=True,
                    )
                )

        db.commit()
        print(f"Seeded {len(DEMO_ISSUES)} demo issues (repo scope: {repo_owner}/{repo_name}).")
        print("Refresh the dashboard to see MTTR, merge rate, success rate, throughput, and cost populated.")


if __name__ == "__main__":
    seed()
