import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    commit_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reveal_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    commitments: Mapped[list["Commitment"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class Commitment(Base):
    __tablename__ = "commitments"
    __table_args__ = (
        UniqueConstraint("event_id", "commitment_hash", name="uq_commitments_event_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    commitment_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    event: Mapped[Event] = relationship(back_populates="commitments")
    reveal: Mapped[Optional["Reveal"]] = relationship(back_populates="commitment", uselist=False)


class Reveal(Base):
    __tablename__ = "reveals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    # 每个承诺至多被揭示一次：唯一约束保证并发下「仅首次匹配的揭示生效」
    commitment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("commitments.id", ondelete="CASCADE"), unique=True
    )
    original_text: Mapped[str] = mapped_column(Text)
    salt: Mapped[str] = mapped_column(Text)
    recomputed_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    commitment: Mapped[Commitment] = relationship(back_populates="reveal")
