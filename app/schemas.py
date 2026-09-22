from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

COMMITMENT_HASH_PATTERN = r"^[0-9a-fA-F]{64}$"


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    commit_deadline: datetime = Field(description="提交截止时间（须含时区，建议 UTC）")
    reveal_deadline: datetime = Field(description="揭示截止时间（须含时区，建议 UTC）")

    @field_validator("commit_deadline", "reveal_deadline")
    @classmethod
    def must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("截止时间必须携带时区偏移（建议使用 UTC，如 2030-01-01T00:00:00Z）")
        return v


class CommitmentCreate(BaseModel):
    commitment_hash: str = Field(
        pattern=COMMITMENT_HASH_PATTERN,
        description="SHA-256 承诺值，64 位十六进制",
        examples=["9f2c..."],
    )


class CommitmentBatchCreate(BaseModel):
    # 元素不在这里做 pattern 校验：格式非法是逐项冲突（409）而非请求体 422
    commitment_hashes: list[str] = Field(
        min_length=1,
        max_length=100,
        description="1～100 个 SHA-256 承诺值（64 位十六进制），按输入顺序在同一事务中原子提交",
    )


class RevealCreate(BaseModel):
    original_text: str = Field(description="预测原文（UTF-8）")
    salt: str = Field(min_length=1, max_length=256, description="随机盐（UTF-8）")


class EventOut(BaseModel):
    id: UUID
    title: str
    description: Optional[str]
    commit_deadline: datetime
    reveal_deadline: datetime
    created_at: datetime
    status: str  # commit | reveal | closed
    commitment_count: int
    reveal_count: int


class CommitmentOut(BaseModel):
    id: UUID
    event_id: UUID
    commitment_hash: str
    created_at: datetime
    revealed: bool


class CommitmentBatchOut(BaseModel):
    count: int = Field(description="本次创建的承诺数量（等于输入哈希数量）")
    commitments: list[CommitmentOut] = Field(description="按输入顺序创建的承诺记录")


class RevealOut(BaseModel):
    id: UUID
    event_id: UUID
    commitment_id: UUID
    commitment_hash: str
    recomputed_hash: str
    hash_scheme: str
    revealed_at: datetime
    verified: bool


class TimelineEntry(BaseModel):
    seq: int
    type: str  # event_created | commitment_submitted | reveal_accepted
    at: datetime
    detail: dict[str, Any]


class TimelineOut(BaseModel):
    event_id: UUID
    status: str
    generated_at: datetime
    hash_scheme: str
    entries: list[TimelineEntry]


class ConflictRequest(BaseModel):
    event_ids: list[UUID] = Field(
        default_factory=list,
        description="待分析的事件 ID 列表（重复 ID 自动去重，空列表返回空结果）",
    )


class ConflictSegmentOut(BaseModel):
    start: datetime = Field(description="片段起点（含，UTC）")
    end: datetime = Field(description="片段终点（不含，UTC，半开边界）")
    phase: str  # commit | reveal
    event_ids: list[UUID] = Field(description="该时段同时处于同一阶段的事件 ID（已排序）")


class ConflictsOut(BaseModel):
    generated_at: datetime
    phases: list[str]
    count: int = Field(description="冲突片段数量")
    segments: list[ConflictSegmentOut]


class HashOut(BaseModel):
    commitment_hash: str
    hash_scheme: str
