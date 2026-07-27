# Devin Remediation Orchestrator

An event-driven system that uses the [Devin API](https://docs.devin.ai) to remediate real issues
found in a fork of [Apache Superset](https://github.com/apache/superset). Findings are identified
and filed as GitHub issues, a human triages them by applying a label, and a FastAPI orchestrator
drives Devin through session creation, CI-aware review-fix loops, and merge -- while a live
dashboard reports MTTR, merge rate, success rate, throughput, and cost.

## Architecture

```
Finding identified (vulnerability / dependency CVE / code-quality issue)
      |  GitHub issue opened (untagged / "needs-triage")
      v
   HUMAN TRIAGE GATE  <- deliberate approval point; human applies `devin-fix` label
      v  (issue labeled `devin-fix`)
GitHub emits "issue labeled" event
      |  signed webhook (HMAC)
      v
+----------------------- FastAPI ORCHESTRATOR (Docker) -----------------------+
|  Webhook intake   -> HMAC verify + dedup                                     |
|  Session manager  -> create V3 Devin session (prompt + tag + ACU cap)        |
|  Lifecycle loop   -> poll status; review-fix on CI red; escalate/terminate   |
|  Metrics + store  -> record every transition w/ timestamps; compute metrics  |
+-------------------------------------------------------------------------------+
      |  create/message/poll/terminate (Devin API)   |  serves JSON
      v                                                v
Devin cloud sandbox -> fixes, runs tests, opens PR    Live dashboard
      |                                                (MTTR, merge/success rate, throughput, cost)
      v
PR -> GitHub Actions CI -> merge -> closes issue
```

### What's built

- Datastore + schema: `issues`, `sessions`, `transitions`, `webhook_deliveries`.
- `/webhook`: HMAC-verified GitHub webhook intake with two-layer dedup (delivery-id + time-bounded
  same issue+action+label guard). On `issues`/`labeled` with label `devin-fix`, it hands off to
  `app/session_manager.py` via `BackgroundTasks`, which creates a **real V3 Devin session**
  (prompt + tag + ACU cap), gated by its own idempotency check (skip if a non-terminal session
  already exists for the issue) and a concurrency cap.
- `app/poller.py`: a genuinely long-running `asyncio` task (started/stopped from `main.py`'s
  lifespan, not `BackgroundTasks`) that polls session status, tracks each PR's CI state via
  `app/github_client.py`, runs the review-fix loop (message the session on CI-red, deduped by
  failure signature so it never re-nags an unchanged failure), merges on CI-green, and escalates
  on ACU ratio / max age / hard-stop quota errors / repeated poll failures.
- `/metrics/summary` and `/metrics/timeseries`: real queries over the schema (MTTR, merge rate,
  success rate, throughput, cost, triage dismissals) plus Devin's own org-level analytics joined
  in, returning clean `null`/`[]` on an empty store rather than fabricated zeros.
- A live dashboard (`/`) polling both endpoints every 5s.
- 55 orchestrator-level tests: HMAC accept/reject, dedup (both layers), session manager
  idempotency/concurrency, poller lifecycle (CI states, message dedup, merge idempotency,
  escalation triggers, terminal resolution ordering), metrics.
- Verified live end-to-end (not just unit tests) against the real Superset fork: issue labeled ->
  webhook -> session created -> PR opened -> CI -> outcome, tags visible in the Devin dashboard.

## Quickstart

```bash
cp .env.example .env
docker compose up --build orchestrator
```

- API docs: http://localhost:8000/docs
- Live dashboard: http://localhost:8000/
- Health check: http://localhost:8000/health

The SQLite file lives in a named Docker volume (`sqlite-data`), not a host bind mount -- this keeps
the compose file portable across machines with different Docker Desktop file-sharing configs. To
inspect it directly: `docker compose exec orchestrator sqlite3 /app/data/orchestrator.db`.

## Running tests

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

## Webhook local dev

GitHub can't reach `localhost` directly -- use a tunnel (`ngrok http 8000` or
`gh webhook forward`) pointed at `localhost:8000/webhook`, and register that public URL plus
`WEBHOOK_SECRET` as the repo's webhook. The test suite exercises the handler entirely through
FastAPI's `TestClient`, so no tunnel is needed to run `pytest`.

## Simulating the workflow without a Devin key

`app/devin_client.py` wraps the real Devin API and is what the running system uses. A
`DEVIN_MOCK=true` env flag that swaps in canned session responses (created -> running -> finished
with a fake PR URL), so reviewers without a Devin key can still see the full webhook -> session ->
metrics loop end to end, is not built yet -- this section documents the intended seam.

## Schema & metrics

- **`issues`** -- one row per GitHub issue, denormalized for display (`github_created_at` is a
  convenience copy, never used for metrics).
- **`sessions`** -- one row per Devin session: status, lifecycle stage, ACU used/cap, whether the
  goal was achieved (from structured output, never inferred from `status` alone).
- **`transitions`** -- append-only event log and **the single source of truth for every metric**:
  `issue_created`, `session_started`, `pr_opened`, `ci_result`, `merged`, each with a real
  `occurred_at` timestamp.
- **`webhook_deliveries`** -- dedup ledger keyed on GitHub's `X-GitHub-Delivery` header, plus the
  fields needed for the time-bounded secondary dedup check.

Metrics (`app/metrics.py`), all computed from `transitions`/`sessions`:

| Metric | Definition |
|---|---|
| MTTR | mean(`merged_at` - `issue_created_at`) in hours, over issues with both events |
| Merge rate | distinct merged issues / distinct opened issues |
| Success rate | sessions with `goal_achieved` / total sessions |
| Throughput | merged transitions in the last 7 days / 7 |
| Cost | sum of `acu_used` across sessions |

## Environment variables

See `.env.example`. All of `DEVIN_API_KEY`, `DEVIN_ORG_ID`, `GITHUB_TOKEN`, `GITHUB_REPO_OWNER`,
and `GITHUB_REPO_NAME` are live and required -- the running system is wired to a real Devin org and
a real GitHub fork, not stub credentials.
