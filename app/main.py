"""封存并按时公开预测内容的 Commit-Reveal 服务。

生命周期（所有时间点均按服务端 UTC 判定）：
  [创建, commit_deadline)   commit 阶段：可提交承诺
  [commit_deadline, reveal_deadline)  reveal 阶段：可揭示
  [reveal_deadline, ∞)      closed：只读
"""

import logging
import re
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
    COMMITMENT_HASH_PATTERN,
    CommitmentBatchCreate,
    CommitmentBatchOut,
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

# 批量提交逐项冲突类型
INVALID_FORMAT = "INVALID_FORMAT"
DUPLICATE_IN_REQUEST = "DUPLICATE_IN_REQUEST"
DUPLICATE_COMMITMENT = "DUPLICATE_COMMITMENT"

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


def api_error(
    status_code: int,
    code: str,
    message: str,
    conflicts: list[dict] | None = None,
) -> HTTPException:
    detail: dict = {"code": code, "message": message}
    if conflicts is not None:
        detail["conflicts"] = conflicts
    return HTTPException(status_code=status_code, detail=detail)


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


@app.post(
    "/events/{event_id}/commitments/batch",
    status_code=201,
    response_model=CommitmentBatchOut,
)
def submit_commitments_batch(
    event_id: UUID, payload: CommitmentBatchCreate, db: Session = Depends(get_db)
):
    """在单个数据库事务中原子批量提交 1～100 个承诺。

    所有写入前的校验按输入顺序完成，任何一项冲突即整体回滚（不留下
    部分承诺），并返回 409 与逐项冲突清单：
      - INVALID_FORMAT：不是 64 位十六进制哈希
      - DUPLICATE_IN_REQUEST：在本次请求内部重复（第二次及以后出现的下标）
      - DUPLICATE_COMMITMENT：数据库中该事件已有此哈希
    格式非法优先于重复判定；冲突清单按下标升序，每个下标至多一项。
    全部通过时按输入顺序创建，并以相同顺序返回记录。
    """
    event = get_event_or_404(db, event_id)
    now = now_utc()
    commit_dl = as_utc(event.commit_deadline)
    if now >= commit_dl:
        raise api_error(
            409,
            "COMMIT_PHASE_CLOSED",
            f"提交截止时间为 {commit_dl.isoformat()}，当前服务端时间 {now.isoformat()}，迟到提交被拒绝",
        )

    raw_hashes = payload.commitment_hashes
    normalized = [h.lower() for h in raw_hashes]
    conflicts: list[dict] = []

    # 1) 按输入顺序校验格式
    valid_format: list[bool] = []
    for i, h in enumerate(raw_hashes):
        ok = re.fullmatch(COMMITMENT_HASH_PATTERN, h) is not None
        valid_format.append(ok)
        if not ok:
            conflicts.append({"index": i, "type": INVALID_FORMAT, "commitment_hash": h})

    # 2) 按输入顺序检查请求内重复（仅对格式合法的项；保留首次出现）
    seen_in_request: set[str] = set()
    first_index: dict[str, int] = {}  # 归一化哈希 -> 请求内首次出现下标
    for i, norm in enumerate(normalized):
        if not valid_format[i]:
            continue
        if norm in seen_in_request:
            conflicts.append(
                {"index": i, "type": DUPLICATE_IN_REQUEST, "commitment_hash": raw_hashes[i]}
            )
            continue
        seen_in_request.add(norm)
        first_index[norm] = i

    # 3) 同事务内一次性查询数据库中已存在的哈希；仅在请求内首次出现的
    #    下标上报库冲突，后续重复已归为 DUPLICATE_IN_REQUEST
    if first_index:
        existing_rows = (
            db.query(Commitment.commitment_hash)
            .filter(
                Commitment.event_id == event.id,
                Commitment.commitment_hash.in_(list(first_index)),
            )
            .all()
        )
        existing = {row[0] for row in existing_rows}
        for norm, i in first_index.items():
            if norm in existing:
                conflicts.append(
                    {"index": i, "type": DUPLICATE_COMMITMENT, "commitment_hash": raw_hashes[i]}
                )

    if conflicts:
        # 尚未执行任何 INSERT：显式回滚以结束事务，保证不留任何写入
        db.rollback()
        conflicts.sort(key=lambda c: c["index"])
        raise api_error(
            409,
            "BATCH_COMMITMENT_CONFLICT",
            f"批量提交包含 {len(conflicts)} 个冲突项，已整体回滚，未创建任何承诺",
            conflicts=conflicts,
        )

    # 4) 全部通过：按输入顺序在同一事务中创建
    commitments = [
        Commitment(event_id=event.id, commitment_hash=norm) for norm in normalized
    ]
    db.add_all(commitments)
    try:
        db.commit()
    except IntegrityError:
        # 并发竞争：预检后、提交前另一事务写入了相同哈希。回滚并重新
        # 读取冲突项，依旧给出逐项清单，保证不留下部分承诺
        db.rollback()
        existing_rows = (
            db.query(Commitment.commitment_hash)
            .filter(
                Commitment.event_id == event.id,
                Commitment.commitment_hash.in_(normalized),
            )
            .all()
        )
        existing = {row[0] for row in existing_rows}
        race_conflicts = [
            {"index": i, "type": DUPLICATE_COMMITMENT, "commitment_hash": raw_hashes[i]}
            for i, norm in enumerate(normalized)
            if norm in existing
        ]
        race_conflicts.sort(key=lambda c: c["index"])
        raise api_error(
            409,
            "BATCH_COMMITMENT_CONFLICT",
            f"批量提交包含 {len(race_conflicts)} 个冲突项，已整体回滚，未创建任何承诺",
            conflicts=race_conflicts,
        )

    return CommitmentBatchOut(
        count=len(commitments),
        commitments=[commitment_out(c) for c in commitments],
    )


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
