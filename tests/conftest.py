import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import Base, get_db
from app.main import app

WEBHOOK_SECRET = "test-secret"


def sign_payload(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def make_issue_payload(
    *,
    issue_id: int,
    issue_number: int,
    label: str | None = "needs-triage",
    action: str = "labeled",
    title: str = "cryptography==3.4.7 has a known CVE",
    body: str = "pip-audit found a known vulnerability, fixed in 41.0.0.",
    state: str = "open",
    created_at: str = "2026-07-20T10:00:00Z",
    repo_owner: str = "kenji-shima",
    repo_name: str = "superset",
) -> bytes:
    payload = {
        "action": action,
        "issue": {
            "id": issue_id,
            "number": issue_number,
            "title": title,
            "body": body,
            "state": state,
            "created_at": created_at,
        },
        "repository": {"name": repo_name, "owner": {"login": repo_owner}},
    }
    if label is not None:
        payload["label"] = {"name": label}
    return json.dumps(payload).encode()


@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def session_factory(db_engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=db_engine)


@pytest.fixture()
def db_session(session_factory):
    session = session_factory()
    yield session
    session.close()


@pytest.fixture()
def client(session_factory, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", WEBHOOK_SECRET)
    # Force these blank regardless of a real .env file on disk (pydantic-settings
    # reads it by default) -- otherwise tests would silently make live network
    # calls to the real Devin API using real credentials. Tests that want to
    # exercise the "configured" path set their own via monkeypatch.setenv.
    monkeypatch.setenv("DEVIN_API_KEY", "")
    monkeypatch.setenv("DEVIN_ORG_ID", "")
    # Explicit rather than left to whatever .env happens to exist on disk --
    # matches make_issue_payload/_make_issue's own defaults so the repo-scoping
    # filter in app/metrics.py behaves the same in every environment.
    monkeypatch.setenv("GITHUB_REPO_OWNER", "kenji-shima")
    monkeypatch.setenv("GITHUB_REPO_NAME", "superset")
    get_settings.cache_clear()

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    # Not used as a context manager: entering it would run the app's lifespan
    # (table creation against the real production engine), which tests must
    # never touch -- they build their own in-memory schema via db_engine.
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture()
def issues_labeled_payload() -> bytes:
    return make_issue_payload(issue_id=9001, issue_number=99, label="devin-fix")


@pytest.fixture()
def patched_session_locals(session_factory, monkeypatch):
    """session_manager and poller each open their own fresh SessionLocal()
    rather than depending on FastAPI's request-scoped get_db (see their
    module docstrings for why) -- point that name at the test's in-memory
    engine so their writes are visible to test assertions.
    """
    import app.poller
    import app.session_manager

    monkeypatch.setattr(app.session_manager, "SessionLocal", session_factory)
    monkeypatch.setattr(app.poller, "SessionLocal", session_factory)
    return session_factory
