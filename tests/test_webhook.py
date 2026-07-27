from datetime import datetime, timedelta

from app.models import Issue, Transition, WebhookDelivery, utcnow
from tests.conftest import make_issue_payload, sign_payload


def _headers(body: bytes, delivery_id: str, event: str = "issues", secret: str | None = None) -> dict:
    return {
        "X-Hub-Signature-256": sign_payload(body, secret) if secret else sign_payload(body),
        "X-GitHub-Delivery": delivery_id,
        "X-GitHub-Event": event,
    }


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_webhook_accepts_valid_signature(client, db_session):
    body = make_issue_payload(issue_id=1001, issue_number=1, label="devin-fix")
    resp = client.post("/webhook", content=body, headers=_headers(body, "d-1"))
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    issue = db_session.query(Issue).filter_by(github_issue_id=1001).one()
    assert issue.label == "devin-fix"
    assert issue.github_created_at == datetime(2026, 7, 20, 10, 0, 0)


def test_webhook_rejects_missing_signature(client):
    body = make_issue_payload(issue_id=1002, issue_number=2)
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Delivery": "d-2", "X-GitHub-Event": "issues"},
    )
    assert resp.status_code == 401


def test_webhook_rejects_invalid_signature(client):
    body = make_issue_payload(issue_id=1003, issue_number=3)
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": "sha256=deadbeef",
            "X-GitHub-Delivery": "d-3",
            "X-GitHub-Event": "issues",
        },
    )
    assert resp.status_code == 401


def test_webhook_rejects_tampered_body(client):
    signed_body = make_issue_payload(issue_id=1004, issue_number=4)
    tampered_body = make_issue_payload(issue_id=1004, issue_number=4, label="devin-fix")
    resp = client.post(
        "/webhook",
        content=tampered_body,
        headers=_headers(signed_body, "d-4"),
    )
    assert resp.status_code == 401


def test_webhook_dedup_same_delivery_id(client, db_session):
    body = make_issue_payload(issue_id=1005, issue_number=5, label="needs-triage")
    headers = _headers(body, "same-delivery")

    r1 = client.post("/webhook", content=body, headers=headers)
    r2 = client.post("/webhook", content=body, headers=headers)

    assert r1.json() == {"status": "ok"}
    assert r2.json() == {"status": "duplicate"}

    issues = db_session.query(Issue).filter_by(github_issue_id=1005).all()
    assert len(issues) == 1
    transitions = db_session.query(Transition).filter_by(
        issue_id=issues[0].id, type="issue_created"
    ).all()
    assert len(transitions) == 1


def test_webhook_dedup_time_bounded_same_issue_action(client, db_session):
    body = make_issue_payload(issue_id=1006, issue_number=6, label="needs-triage")

    r1 = client.post("/webhook", content=body, headers=_headers(body, "delivery-a"))
    r2 = client.post("/webhook", content=body, headers=_headers(body, "delivery-b"))

    assert r1.json() == {"status": "ok"}
    assert r2.json() == {"status": "duplicate"}

    issue = db_session.query(Issue).filter_by(github_issue_id=1006).one()
    transitions = db_session.query(Transition).filter_by(
        issue_id=issue.id, type="issue_created"
    ).all()
    assert len(transitions) == 1


def test_webhook_relabel_after_window_is_not_deduped(client, db_session):
    old_time = utcnow() - timedelta(seconds=60)
    db_session.add(
        WebhookDelivery(
            delivery_id="old-delivery",
            event_type="issues",
            action="labeled",
            github_issue_id=1007,
            label_name="needs-triage",
            received_at=old_time,
        )
    )
    db_session.commit()

    body = make_issue_payload(issue_id=1007, issue_number=7, label="needs-triage")
    resp = client.post("/webhook", content=body, headers=_headers(body, "new-delivery"))

    assert resp.json() == {"status": "ok"}
    issue = db_session.query(Issue).filter_by(github_issue_id=1007).one()
    assert issue.label == "needs-triage"


def test_webhook_ignores_unhandled_event_or_action(client, db_session):
    body = make_issue_payload(issue_id=1008, issue_number=8, action="opened", label=None)
    resp = client.post("/webhook", content=body, headers=_headers(body, "d-8"))

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert db_session.query(Issue).filter_by(github_issue_id=1008).first() is None
    delivery = db_session.query(WebhookDelivery).filter_by(delivery_id="d-8").one()
    assert delivery.action == "opened"


def test_webhook_records_issue_created_transition_once(client, db_session):
    body1 = make_issue_payload(issue_id=1009, issue_number=9, label="needs-triage")
    body2 = make_issue_payload(issue_id=1009, issue_number=9, label="devin-fix")

    r1 = client.post("/webhook", content=body1, headers=_headers(body1, "d-9a"))
    r2 = client.post("/webhook", content=body2, headers=_headers(body2, "d-9b"))

    assert r1.json() == {"status": "ok"}
    assert r2.json() == {"status": "ok"}

    issue = db_session.query(Issue).filter_by(github_issue_id=1009).one()
    assert issue.label == "devin-fix"
    transitions = db_session.query(Transition).filter_by(
        issue_id=issue.id, type="issue_created"
    ).all()
    assert len(transitions) == 1
