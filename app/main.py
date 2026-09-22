"""封存并按时公开预测内容的 Commit-Reveal 服务。

生命周期（所有时间点均按服务端 UTC 判定）：
  [创建, commit_deadline)   commit 阶段：可提交承诺
  [commit_deadline, reveal_deadline)  reveal 阶段：可揭示
  [reveal_deadline, ∞)      closed：只读
"""

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .conflicts import COMMIT, REVEAL, PhaseWindow, sweep_conflicts
from .database import Base, engine, get_db
from .hashing import HASH_SCHEME, compute_commitment
from .models import Commitment, Event, Reveal
from .schemas import (
    CommitmentCreate,
    CommitmentOut,
    ConflictRequest,
    ConflictsOut,
    EventCreate,
    EventOut,
    HashOut,
    RevealCreate,
    RevealOut,
    TimelineOut,
)

logger = logging.getLogger("sealed-predictions")


def now_utc() -> datetime:
    """服务端当前 UTC 时间，全部截止判定以此为唯一依据。"""
    return datetime.now(timezone.utc)


def as_utc(dt: datetime) -> datetime:
    """把数据库读出的时间统一为带 UTC 时区的时间（兼容朴素时间）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def event_status(event: Event, now: datetime) -> str:
    if now < as_utc(event.commit_deadline):
        return "commit"
    if now < as_utc(event.reveal_deadline):
        return "reveal"
    return "closed"


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时建表；数据库尚未就绪时重试，等待 Compose 中的 Postgres 起来
    attempts = 30
    for attempt in range(1, attempts + 1):
        try:
            Base.metadata.create_all(bind=engine)
            logger.info("database schema ready")
            break
        except OperationalError:
            if attempt == attempts:
                raise
            logger.warning("database not ready, retrying (%s/%s)", attempt, attempts)
            time.sleep(1)
    yield


app = FastAPI(
    title="Sealed Predictions",
    description="封存并按时公开预测内容的 Commit-Reveal 服务",
    version="1.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #

def get_event_or_404(db: Session, event_id: UUID) -> Event:
    event = db.get(Event, event_id)
    if event is None:
        raise api_error(404, "EVENT_NOT_FOUND", f"事件 {event_id} 不存在")
    return event


def event_out(event: Event, db: Session, now: datetime) -> EventOut:
    commitment_count = db.query(Commitment).filter(Commitment.event_id == event.id).count()
    reveal_count = db.query(Reveal).filter(Reveal.event_id == event.id).count()
    return EventOut(
        id=event.id,
        title=event.title,
        description=event.description,
        commit_deadline=as_utc(event.commit_deadline),
        reveal_deadline=as_utc(event.reveal_deadline),
        created_at=as_utc(event.created_at),
        status=event_status(event, now),
        commitment_count=commitment_count,
        reveal_count=reveal_count,
    )


def commitment_out(commitment: Commitment) -> CommitmentOut:
    return CommitmentOut(
        id=commitment.id,
        event_id=commitment.event_id,
        commitment_hash=commitment.commitment_hash,
        created_at=as_utc(commitment.created_at),
        revealed=commitment.reveal is not None,
    )


# --------------------------------------------------------------------------- #
# 基础端点
# --------------------------------------------------------------------------- #

@app.get("/")
def root():
    return {
        "service": "sealed-predictions",
        "docs": "/docs",
        "hash_scheme": HASH_SCHEME,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/utils/commitment", response_model=HashOut)
def compute_hash(payload: RevealCreate):
    """按约定方案在服务端计算承诺哈希，便于客户端核对本地实现。"""
    return HashOut(
        commitment_hash=compute_commitment(payload.original_text, payload.salt),
        hash_scheme=HASH_SCHEME,
    )


# --------------------------------------------------------------------------- #
# 事件
# --------------------------------------------------------------------------- #

@app.post("/events", status_code=201, response_model=EventOut)
def create_event(payload: EventCreate, db: Session = Depends(get_db)):
    commit_dl = as_utc(payload.commit_deadline)
    reveal_dl = as_utc(payload.reveal_deadline)
    now = now_utc()
    if reveal_dl <= commit_dl:
        raise api_error(400, "INVALID_DEADLINES", "reveal_deadline 必须晚于 commit_deadline")
    if commit_dl <= now:
        raise api_error(400, "INVALID_DEADLINES", "commit_deadline 必须晚于当前服务端 UTC 时间")
    event = Event(
        title=payload.title,
        description=payload.description,
        commit_deadline=commit_dl,
        reveal_deadline=reveal_dl,
    )
    db.add(event)
    db.commit()
    return event_out(event, db, now)


@app.get("/events", response_model=list[EventOut])
def list_events(db: Session = Depends(get_db)):
    now = now_utc()
    events = db.query(Event).order_by(Event.created_at, Event.id).all()
    return [event_out(e, db, now) for e in events]


@app.get("/events/{event_id}", response_model=EventOut)
def get_event(event_id: UUID, db: Session = Depends(get_db)):
    return event_out(get_event_or_404(db, event_id), db, now_utc())


# --------------------------------------------------------------------------- #
# 事件窗口冲突分析
# --------------------------------------------------------------------------- #

@app.post("/events/conflicts", response_model=ConflictsOut)
def analyze_event_conflicts(payload: ConflictRequest, db: Session = Depends(get_db)):
    """用 sweep-line 计算多个事件的阶段窗口冲突。

    每个事件按服务端 UTC 视为两段半开区间：
      commit: [created_at, commit_deadline)
      reveal: [commit_deadline, reveal_deadline)

    返回任意时刻同时处于**同一**阶段（commit 或 reveal）的事件集合所
    覆盖的时间片段；片段按起止时间与阶段排序。请求中的重复 ID 去重；
    任何 ID 不存在均返回 404；空列表稳定返回空片段集。
    """
    now = now_utc()
    # 保序去重：同一事件只参与一次分析
    event_ids = list(dict.fromkeys(payload.event_ids))

    events_by_id: dict[UUID, Event] = {}
    if event_ids:
        events = db.query(Event).filter(Event.id.in_(event_ids)).all()
        events_by_id = {e.id: e for e in events}
    missing = next((eid for eid in event_ids if eid not in events_by_id), None)
    if missing is not None:
        raise api_error(404, "EVENT_NOT_FOUND", f"事件 {missing} 不存在")

    windows: list[PhaseWindow] = []
    for event in (events_by_id[eid] for eid in event_ids):
        created = as_utc(event.created_at)
        commit_dl = as_utc(event.commit_deadline)
        reveal_dl = as_utc(event.reveal_deadline)
        windows.append(PhaseWindow(event.id, COMMIT, created, commit_dl))
        windows.append(PhaseWindow(event.id, REVEAL, commit_dl, reveal_dl))

    segments = sweep_conflicts(windows)
    return ConflictsOut(
        generated_at=now,
        phases=[COMMIT, REVEAL],
        count=len(segments),
        segments=[
            {
                "start": s.start,
                "end": s.end,
                "phase": s.phase,
                "event_ids": list(s.event_ids),
            }
            for s in segments
        ],
    )


# --------------------------------------------------------------------------- #
# 承诺
# --------------------------------------------------------------------------- #

@app.post("/events/{event_id}/commitments", status_code=201, response_model=CommitmentOut)
def submit_commitment(event_id: UUID, payload: CommitmentCreate, db: Session = Depends(get_db)):
    event = get_event_or_404(db, event_id)
    now = now_utc()
    commit_dl = as_utc(event.commit_deadline)
    if now >= commit_dl:
        raise api_error(
            409,
            "COMMIT_PHASE_CLOSED",
            f"提交截止时间为 {commit_dl.isoformat()}，当前服务端时间 {now.isoformat()}，迟到提交被拒绝",
        )
    commitment = Commitment(event_id=event.id, commitment_hash=payload.commitment_hash.lower())
    db.add(commitment)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(409, "DUPLICATE_COMMITMENT", "该承诺哈希已在此事件中提交过")
    return commitment_out(commitment)


@app.get("/events/{event_id}/commitments", response_model=list[CommitmentOut])
def list_commitments(event_id: UUID, db: Session = Depends(get_db)):
    event = get_event_or_404(db, event_id)
    commitments = (
        db.query(Commitment)
        .filter(Commitment.event_id == event.id)
        .order_by(Commitment.created_at, Commitment.id)
        .all()
    )
    return [commitment_out(c) for c in commitments]


# --------------------------------------------------------------------------- #
# 揭示
# --------------------------------------------------------------------------- #

@app.post("/events/{event_id}/reveals", status_code=201, response_model=RevealOut)
def submit_reveal(event_id: UUID, payload: RevealCreate, db: Session = Depends(get_db)):
    event = get_event_or_404(db, event_id)
    now = now_utc()
    commit_dl = as_utc(event.commit_deadline)
    reveal_dl = as_utc(event.reveal_deadline)
    if now < commit_dl:
        raise api_error(
            409,
            "REVEAL_PHASE_NOT_STARTED",
            f"揭示期自 {commit_dl.isoformat()} 开始，当前服务端时间 {now.isoformat()}，提前揭示被拒绝",
        )
    if now >= reveal_dl:
        raise api_error(
            409,
            "REVEAL_PHASE_CLOSED",
            f"揭示截止时间为 {reveal_dl.isoformat()}，当前服务端时间 {now.isoformat()}，逾期揭示被拒绝",
        )

    recomputed = compute_commitment(payload.original_text, payload.salt)
    commitment = (
        db.query(Commitment)
        .filter(Commitment.event_id == event.id, Commitment.commitment_hash == recomputed)
        .first()
    )
    if commitment is None:
        raise api_error(
            409,
            "NO_MATCHING_COMMITMENT",
            "原文与盐重算的哈希不匹配本事件中的任何承诺",
        )
    if commitment.reveal is not None:
        raise api_error(
            409,
            "DUPLICATE_REVEAL",
            "该承诺已被揭示，仅首次匹配的揭示生效",
        )

    reveal = Reveal(
        event_id=event.id,
        commitment_id=commitment.id,
        original_text=payload.original_text,
        salt=payload.salt,
        recomputed_hash=recomputed,
    )
    db.add(reveal)
    try:
        db.commit()
    except IntegrityError:
        # 并发下另一请求已抢先揭示同一承诺：回滚，状态不变
        db.rollback()
        raise api_error(409, "DUPLICATE_REVEAL", "该承诺已被揭示，仅首次匹配的揭示生效")

    return RevealOut(
        id=reveal.id,
        event_id=event.id,
        commitment_id=commitment.id,
        commitment_hash=commitment.commitment_hash,
        recomputed_hash=recomputed,
        hash_scheme=HASH_SCHEME,
        revealed_at=as_utc(reveal.created_at),
        verified=True,
    )


# --------------------------------------------------------------------------- #
# 时间线
# --------------------------------------------------------------------------- #

@app.get("/events/{event_id}/timeline", response_model=TimelineOut)
def get_timeline(event_id: UUID, db: Session = Depends(get_db)):
    event = get_event_or_404(db, event_id)
    now = now_utc()

    entries: list[dict] = [
        {
            "type": "event_created",
            "at": as_utc(event.created_at),
            "detail": {
                "title": event.title,
                "commit_deadline": as_utc(event.commit_deadline).isoformat(),
                "reveal_deadline": as_utc(event.reveal_deadline).isoformat(),
            },
        }
    ]

    commitments = (
        db.query(Commitment)
        .filter(Commitment.event_id == event.id)
        .order_by(Commitment.created_at, Commitment.id)
        .all()
    )
    reveals = {r.commitment_id: r for r in db.query(Reveal).filter(Reveal.event_id == event.id)}

    for c in commitments:
        entries.append(
            {
                "type": "commitment_submitted",
                "at": as_utc(c.created_at),
                "detail": {
                    "commitment_id": str(c.id),
                    "commitment_hash": c.commitment_hash,
                },
            }
        )
        r = reveals.get(c.id)
        if r is not None:
            entries.append(
                {
                    "type": "reveal_accepted",
                    "at": as_utc(r.created_at),
                    "detail": {
                        "commitment_id": str(c.id),
                        "commitment_hash": c.commitment_hash,
                        "recomputed_hash": r.recomputed_hash,
                        "original_text": r.original_text,
                        "salt": r.salt,
                        "hash_scheme": HASH_SCHEME,
                    },
                }
            )

    # 稳定排序：同一时间戳下保持 承诺 -> 揭示 的先后
    entries.sort(key=lambda e: e["at"])
    timeline = [
        {"seq": i + 1, "type": e["type"], "at": e["at"], "detail": e["detail"]}
        for i, e in enumerate(entries)
    ]
    return TimelineOut(
        event_id=event.id,
        status=event_status(event, now),
        generated_at=now,
        hash_scheme=HASH_SCHEME,
        entries=timeline,
    )
