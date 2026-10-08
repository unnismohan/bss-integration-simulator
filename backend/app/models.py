from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow():
    return datetime.now(timezone.utc)


class Scenario(Base):
    __tablename__ = "scenarios"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, default=1)
    definition: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ScenarioVersion(Base):
    __tablename__ = "scenario_versions"
    __table_args__ = (UniqueConstraint("scenario_key", "version", name="uq_scenario_version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_key: Mapped[str] = mapped_column(ForeignKey("scenarios.key", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CallbackJob(Base):
    __tablename__ = "callback_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_key: Mapped[str] = mapped_column(String(100), index=True)
    scenario_version: Mapped[int] = mapped_column(Integer)
    correlation_id: Mapped[str] = mapped_column(String(250), index=True)
    callback_url: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    retry_delay_ms: Mapped[int] = mapped_column(Integer, default=1000)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ServiceMetric(Base):
    __tablename__ = "service_metrics"
    service_name: Mapped[str] = mapped_column(String(50), primary_key=True)
    cpu_percent: Mapped[float] = mapped_column(Float, default=0)
    memory_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CallbackWorkerControl(Base):
    __tablename__ = "callback_worker_control"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


Index("ix_callback_jobs_due", CallbackJob.state, CallbackJob.due_at)
