from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(schema="product")


class ProductDefaultsRow(Base):
    __tablename__ = "product_defaults"
    __table_args__ = (CheckConstraint("version > 0", name="defaults_version_positive"),)

    workspace_id: Mapped[UUID] = mapped_column(primary_key=True)
    main_profile_id: Mapped[UUID | None]
    planner_profile_id: Mapped[UUID | None]
    worker_profile_id: Mapped[UUID | None]
    version: Mapped[int]


class BrowserSessionRow(Base):
    __tablename__ = "browser_sessions"

    session_id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID]
    principal_id: Mapped[str] = mapped_column(String(200))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RequestStateRow(Base):
    __tablename__ = "request_state"

    request_id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID]
    principal_id: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
