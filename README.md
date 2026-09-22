# Sealed Predictions — 封存并按时公开预测的 Commit-Reveal 服务

基于 **FastAPI + PostgreSQL** 的预测封存服务：先在截止前提交预测的承诺哈希（SHA-256），
进入揭示期后再公开原文与随机盐，服务端重算哈希校验，**仅首次匹配的揭示生效**，
并提供可核验的事件时间线。所有截止时间均按**服务端 UTC** 判定。

## 快速启动（一条命令）

```bash
docker compose up --build
```

- API 地址：**http://localhost:8000**
- 交互式文档（Swagger UI）：**http://localhost:8000/docs**
- 健康检查：`GET http://localhost:8000/health`

依赖安装与建表均在启动时自动完成：Compose 启动 Postgres 16 与 API 两个容器，
API 等待数据库健康检查后启动，并在启动时自动创建数据表（`events` / `commitments` / `reveals`）。
两个容器均带 healthcheck，`docker compose ps` 中状态变为 `healthy` 即就绪。

## 承诺哈希方案

```
commitment = SHA-256( 原文字节长度的 8 字节大端整数 || 原文 UTF-8 字节 || 盐 UTF-8 字节 )
```

8 字节大端长度前缀用于消除「原文 + 盐」拼接歧义。可用服务端工具端点核对本地实现：

```bash
curl -s -X POST http://localhost:8000/utils/commitment \
  -H 'Content-Type: application/json' \
  -d '{"original_text": "明天下雨", "salt": "my-random-salt"}'
# {"commitment_hash": "...", "hash_scheme": "sha256(u64be(len(utf8(text))) || utf8(text) || utf8(salt))"}
```

## 事件生命周期（服务端 UTC 判定）

| 阶段 | 时间区间 | 允许操作 |
|------|----------|----------|
| `commit` | 创建时刻 ≤ now < commit_deadline | 提交承诺 |
| `reveal` | commit_deadline ≤ now < reveal_deadline | 提交揭示 |
| `closed` | now ≥ reveal_deadline | 只读 |

边界语义：到达 commit_deadline 的瞬间提交通道关闭、揭示通道开启；到达 reveal_deadline 揭示通道关闭。

## API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/events` | 创建事件（commit_deadline 须在未来，reveal_deadline 须更晚） |
| GET | `/events` | 事件列表（含状态与计数） |
| GET | `/events/{id}` | 事件详情 |
| POST | `/events/conflicts` | 批量事件的阶段窗口冲突分析（sweep-line） |
| POST | `/events/{id}/commitments` | 提交承诺（64 位十六进制哈希） |
| POST | `/events/{id}/commitments/batch` | 原子批量提交 1～100 个承诺（单事务，冲突整体回滚） |
| GET | `/events/{id}/commitments` | 承诺列表（含是否已揭示） |
| POST | `/events/{id}/reveals` | 揭示：提交原文 + 盐，服务端重算校验 |
| GET | `/events/{id}/timeline` | 可核验的事件时间线 |
| POST | `/utils/commitment` | 按约定方案计算承诺哈希 |
| GET | `/health` | 健康检查 |

## 完整使用示例

```bash
# 1. 创建事件（截止时间须携带时区）
curl -s -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{"title": "我的预测", "commit_deadline": "2030-01-01T00:00:00Z", "reveal_deadline": "2030-01-02T00:00:00Z"}'
# => {"id": "<EVENT_ID>", "status": "commit", ...}

# 2. 本地计算承诺（Python 参考实现）
python3 - <<'EOF'
import hashlib
text, salt = "明天下雨", "my-random-salt"
tb = text.encode("utf-8")
print(hashlib.sha256(len(tb).to_bytes(8, "big") + tb + salt.encode()).hexdigest())
EOF

# 3. 提交截止前提交承诺
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/commitments \
  -H 'Content-Type: application/json' \
  -d '{"commitment_hash": "<64位十六进制哈希>"}'

# 4. 进入揭示期后提交原文与盐
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/reveals \
  -H 'Content-Type: application/json' \
  -d '{"original_text": "明天下雨", "salt": "my-random-salt"}'
# => {"verified": true, "recomputed_hash": "...", "commitment_hash": "...", ...}

# 5. 获取可核验时间线（含原文与盐，任何人可重算哈希比对）
curl -s http://localhost:8000/events/<EVENT_ID>/timeline
```

## 事件窗口冲突分析（`POST /events/conflicts`）

把每个事件按**服务端 UTC** 视为两段**半开**时间区间，用 sweep-line 计算任意时刻
有哪些事件同时处于**同一阶段**（同为 `commit` 或同为 `reveal`，跨阶段不算冲突）：

```
commit: [created_at, commit_deadline)
reveal: [commit_deadline, reveal_deadline)
```

半开边界语义：到达 `commit_deadline` 的瞬间旧事件已离开 commit 阶段、新事件可恰好
进入——首尾相接（一个区间的右端点等于另一个的左端点）**不产生**冲突。返回的冲突片段
按 `start`、`end`、`phase` 排序，片段内 `event_ids` 升序；无冲突（或请求为空）时
稳定返回空列表。

```bash
curl -s -X POST http://localhost:8000/events/conflicts \
  -H 'Content-Type: application/json' \
  -d '{"event_ids": ["<EVENT_ID_1>", "<EVENT_ID_2>"]}'
```

```json
{
  "generated_at": "2030-06-01T00:00:00Z",
  "phases": ["commit", "reveal"],
  "count": 2,
  "segments": [
    {
      "start": "2030-01-01T01:00:00Z",
      "end": "2030-01-01T02:00:00Z",
      "phase": "commit",
      "event_ids": ["<EVENT_ID_1>", "<EVENT_ID_2>"]
    },
    {
      "start": "2030-01-02T01:00:00Z",
      "end": "2030-01-02T02:00:00Z",
      "phase": "reveal",
      "event_ids": ["<EVENT_ID_1>", "<EVENT_ID_2>"]
    }
  ]
}
```

空结果同样稳定：`POST /events/conflicts -d '{"event_ids": []}'`（或省略字段）返回
`{"generated_at": "...", "phases": ["commit", "reveal"], "count": 0, "segments": []}`。
请求中的重复 ID 自动去重；任一 ID 不存在返回 `404 EVENT_NOT_FOUND`；非 UUID 返回 422。

## 原子批量提交承诺（`POST /events/{id}/commitments/batch`）

在**单个数据库事务**中按输入顺序提交 1～100 个承诺哈希。服务端在写入前完成全部校验，
**任何一项冲突都整体回滚**，不会留下部分承诺：

- 逐项校验哈希格式（64 位十六进制，大小写均可，存储时归一化为小写）；
- 检查请求内重复（第二次及以后出现的下标记为请求内重复，大小写不敏感）；
- 一次性查询数据库中该事件已有的哈希。

冲突时返回 `409 BATCH_COMMITMENT_CONFLICT`，统一错误结构内附 `conflicts` 数组，
每项给出 `index`（从 0 开始的输入下标）、`type` 与提交时的原始哈希，按下标升序、
每个下标至多一项：

| 冲突 type | 含义 |
|-----------|------|
| `INVALID_FORMAT` | 不是 64 位十六进制哈希 |
| `DUPLICATE_IN_REQUEST` | 同一哈希在本次请求中重复（首次出现之后的下标） |
| `DUPLICATE_COMMITMENT` | 数据库中该事件已存在此哈希（请求内首次出现的下标） |

```bash
# 成功：按输入顺序创建，返回 count 与承诺记录（201）
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/commitments/batch \
  -H 'Content-Type: application/json' \
  -d '{"commitment_hashes": [
        "9f2c7fd25e1b3afad3e85a0bd17d9b100db4b3bd306a4f6e5535e23e15e82d7d",
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
      ]}'
# => {"count": 2, "commitments": [{...}, {...}]}

# 冲突：整体回滚（409），逐项指出冲突下标与类型
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/commitments/batch \
  -H 'Content-Type: application/json' \
  -d '{"commitment_hashes": [
        "9f2c7fd25e1b3afad3e85a0bd17d9b100db4b3bd306a4f6e5535e23e15e82d7d",
        "not-a-valid-hash",
        "9f2c7fd25e1b3afad3e85a0bd17d9b100db4b3bd306a4f6e5535e23e15e82d7d"
      ]}'
```

```json
{
  "detail": {
    "code": "BATCH_COMMITMENT_CONFLICT",
    "message": "批量提交包含 2 个冲突项，已整体回滚，未创建任何承诺",
    "conflicts": [
      {"index": 1, "type": "INVALID_FORMAT", "commitment_hash": "not-a-valid-hash"},
      {"index": 2, "type": "DUPLICATE_IN_REQUEST",
       "commitment_hash": "9f2c7fd25e1b3afad3e85a0bd17d9b100db4b3bd306a4f6e5535e23e15e82d7d"}
    ]
  }
}
```

列表为空或超过 100 项返回 422；事件不存在返回 `404 EVENT_NOT_FOUND`；
已过 `commit_deadline` 与单个提交端点一致返回 `409 COMMIT_PHASE_CLOSED`，
以上情形同样不产生任何写入。

## 错误响应

错误统一返回 `{"detail": {"code": "...", "message": "..."}}`，且**均不改变任何状态**
（所有校验先于写入，并发冲突由数据库唯一约束兜底并回滚）：

| HTTP | code | 场景 |
|------|------|------|
| 400 | `INVALID_DEADLINES` | 截止时间顺序非法或 commit_deadline 不在未来 |
| 404 | `EVENT_NOT_FOUND` | 事件不存在（含冲突分析请求中的任一 ID） |
| 409 | `COMMIT_PHASE_CLOSED` | 迟到提交（已过 commit_deadline） |
| 409 | `DUPLICATE_COMMITMENT` | 同一事件内重复的承诺哈希（单个提交端点） |
| 409 | `BATCH_COMMITMENT_CONFLICT` | 批量提交存在逐项冲突（`detail.conflicts` 给出每个冲突项的 index/type/hash） |
| 409 | `REVEAL_PHASE_NOT_STARTED` | 提前揭示（未到 commit_deadline） |
| 409 | `REVEAL_PHASE_CLOSED` | 逾期揭示（已过 reveal_deadline） |
| 409 | `NO_MATCHING_COMMITMENT` | 原文 + 盐重算后不匹配任何承诺 |
| 409 | `DUPLICATE_REVEAL` | 该承诺已被揭示（仅首次匹配生效） |
| 422 | — | 请求体格式校验失败（如哈希非 64 位十六进制） |

「仅首次匹配的揭示生效」由 `reveals.commitment_id` 唯一约束保证：并发揭示同一承诺时，
后到者事务回滚并收到 `DUPLICATE_REVEAL`。

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
DATABASE_URL=sqlite:///./dev.db uvicorn app.main:app --reload   # 本地运行（默认连 PostgreSQL）
python3 -m pytest tests/ -q                                     # 运行测试（SQLite + 模拟时钟）
```

## 项目结构

```
app/
  main.py       # 路由、生命周期、时间判定与错误处理
  conflicts.py  # 阶段窗口 sweep-line 冲突分析（纯函数，可独立单测）
  models.py     # SQLAlchemy 模型（events / commitments / reveals）
  schemas.py    # Pydantic 请求/响应模型
  hashing.py    # 承诺哈希方案实现
  database.py   # 引擎与会话（DATABASE_URL 可配置）
tests/test_api.py        # 业务规则冒烟测试（SQLite + 模拟时钟）
tests/test_conflicts.py  # 冲突分析：sweep-line 边界、排序与 API 用例
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
