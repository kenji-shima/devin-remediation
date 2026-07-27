import pytest

from app import metrics, seed_demo
from app.config import get_settings
from app.models import DevinSession, Issue, Transition


@pytest.fixture()
def patched_seed_session_local(session_factory, monkeypatch):
    monkeypatch.setattr(seed_demo, "SessionLocal", session_factory)
    return session_factory


def test_seed_populates_expected_rows(db_session, patched_seed_session_local):
    seed_demo.seed()

    assert db_session.query(Issue).count() == len(seed_demo.DEMO_ISSUES)
    assert db_session.query(DevinSession).count() == len(seed_demo.DEMO_ISSUES)
    assert db_session.query(Transition).filter(Transition.type == "issue_created").count() == 6
    assert db_session.query(Transition).filter(Transition.type == "merged").count() == 2


def test_seed_produces_real_nonnull_metrics(db_session, patched_seed_session_local):
    seed_demo.seed()

    assert metrics.compute_merge_rate(db_session) == pytest.approx(2 / 6)
    assert metrics.compute_success_rate(db_session) == pytest.approx(1.0)
    assert metrics.compute_triage_dismissals(db_session) == 4
    assert metrics.compute_mttr(db_session) == pytest.approx((1.2 + 0.7) / 2)
    assert metrics.compute_cost(db_session) is not None


def test_seed_is_a_noop_on_a_nonempty_store(db_session, patched_seed_session_local):
    seed_demo.seed()
    seed_demo.seed()

    assert db_session.query(Issue).count() == len(seed_demo.DEMO_ISSUES)


def test_seed_uses_configured_repo_scope(db_session, patched_seed_session_local, monkeypatch):
    monkeypatch.setenv("GITHUB_REPO_OWNER", "some-owner")
    monkeypatch.setenv("GITHUB_REPO_NAME", "some-repo")
    get_settings.cache_clear()

    seed_demo.seed()

    issue = db_session.query(Issue).first()
    assert issue.repo_owner == "some-owner"
    assert issue.repo_name == "some-repo"

    get_settings.cache_clear()
