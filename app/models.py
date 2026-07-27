from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    # Naive UTC throughout: SQLite's DateTime() does not actually
    # persist tzinfo -- it round-trips as naive, so mixing naive/aware here
    # would raise "can't compare offset-naive and offset-aware datetimes".
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Issue(Base):
    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    github_issue_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    github_issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    repo_owner: Mapped[str] = mapped_column(String, nullable=False)
    repo_name: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String, nullable=False, default="open")
    # Denormalized display copy only -- metric queries must read Transition.occurred_at instead.
    github_created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, default=utcnow, onupdate=utcnow
    )

    sessions: Mapped[list["DevinSession"]] = relationship(back_populates="issue")
    transitions: Mapped[list["Transition"]] = relationship(back_populates="issue")


class DevinSession(Base):
    """A Devin session for an issue."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id"), nullable=False)
    devin_session_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    tag: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="created")
    status_detail: Mapped[str | None] = mapped_column(String, nullable=True)
    goal_achieved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Raw resolution verdict: "fixed" | "false_positive" | "blocked", or None
    # while unresolved. A correctly-diagnosed false positive is a genuinely
    # good outcome, not a remediation failure -- goal_achieved alone can't
    # distinguish "we tried to fix it and couldn't" from "there was nothing to
    # fix," so success_rate reads this column to exclude the latter from its
    # denominator rather than counting it as a miss.
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    acu_used: Mapped[float | None] = mapped_column(Float, nullable=True)
    acu_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The poller's own view of whether to keep polling -- deliberately separate
    # from status/status_detail, which stay a pure mirror of Devin's raw API.
    lifecycle_stage: Mapped[str] = mapped_column(String, nullable=False, default="active")
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    escalated_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    consecutive_poll_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, default=utcnow, onupdate=utcnow
    )

    issue: Mapped["Issue"] = relationship(back_populates="sessions")
    transitions: Mapped[list["Transition"]] = relationship(back_populates="session")
    pr_states: Mapped[list["SessionPrState"]] = relationship(back_populates="session")


class Transition(Base):
    """Append-only event log -- the source of truth for all metric computations."""

    __tablename__ = "transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id"), nullable=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id"), nullable=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    acu_used: Mapped[float | None] = mapped_column(Float, nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String, nullable=True)
    transition_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)

    issue: Mapped["Issue"] = relationship(back_populates="transitions")
    session: Mapped["DevinSession"] = relationship(back_populates="transitions")


class SessionPrState(Base):
    """Mutable poller bookkeeping for one (session, PR) pair -- NOT part of the
    append-only Transition log. Its existence for a (session_id, pr_url) pair
    is itself the "have I already recorded pr_opened" answer.
    """

    __tablename__ = "session_pr_states"
    __table_args__ = (UniqueConstraint("session_id", "pr_url", name="uq_session_pr"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    pr_url: Mapped[str] = mapped_column(String, nullable=False)
    head_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    ci_outcome: Mapped[str | None] = mapped_column(String, nullable=True)  # no_ci|pending|pass|fail
    failure_signature: Mapped[str | None] = mapped_column(String, nullable=True)
    last_message_signature_sent: Mapped[str | None] = mapped_column(String, nullable=True)
    pr_resolved_without_merge: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    merged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, default=utcnow, onupdate=utcnow
    )

    session: Mapped["DevinSession"] = relationship(back_populates="pr_states")


class WebhookDelivery(Base):
    """Dedup ledger keyed on GitHub's X-GitHub-Delivery header."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    delivery_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str | None] = mapped_column(String, nullable=True)
    github_issue_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    label_name: Mapped[str | None] = mapped_column(String, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
