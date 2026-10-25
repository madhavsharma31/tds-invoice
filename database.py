
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import (
    create_engine,
    String,
    Text,
    DateTime,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

load_dotenv()


def utc_now():
    return datetime.now(timezone.utc)


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./invoice_agent.db",
)

engine_options = {}

if DATABASE_URL.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False}

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    **engine_options,
)


class Base(DeclarativeBase):
    pass


class TaskRecord(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    principal: Mapped[str] = mapped_column(String(64), index=True)
    context_id: Mapped[str] = mapped_column(String(128), index=True)
    batch_id: Mapped[str] = mapped_column(String(256), index=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    task_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class MessageRecord(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    principal: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[str] = mapped_column(String(256))
    content_hash: Mapped[str] = mapped_column(String(64))
    task_id: Mapped[str] = mapped_column(String(128), index=True)

    __table_args__ = (
        UniqueConstraint(
            "principal",
            "message_id",
            name="uq_message_principal_message",
        ),
    )


class DecisionCacheRecord(Base):
    __tablename__ = "decision_cache"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class ActionExecutionRecord(Base):
    __tablename__ = "action_executions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(128))
    package_id: Mapped[str] = mapped_column(String(256))
    action_id: Mapped[str] = mapped_column(String(256))
    receipt_nonce: Mapped[str] = mapped_column(String(512))
    execution_json: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "action_id",
            name="uq_execution_task_action",
        ),
        Index("ix_execution_task", "task_id"),
    )


def create_tables():
    Base.metadata.create_all(bind=engine)


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)
