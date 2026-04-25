# Coordinator Service SPEC (2026-04-25)

> 同机简化版 of TG-MTProto Round 3 提议. A-sibling 接手实施 (Step 6).

## 1. Why

- A (mobile-auto0423) 跑 facebook 任务, B (telegram-mtproto-ai) 跑 telegram/LINE/独立 messenger 任务
- 21 台真机共享 (19 Redmi A 原有 + 2 bg_phone_{1,2} B 搬来)
- A 的 `fb_concurrency.messenger_active` 是纯内存 `threading.Lock`, 不可跨进程
- 同设备同 section 两 repo runner 同时跑会抢 ADB session + 输入框焦
- 需要**跨进程跨 repo 锁 + 设备注册中心**

## 2. 同机简化 (vs TG R3 跨机方案)

- **localhost only**: listen 127.0.0.1:9810, 不开公网, 无 auth (省 actor token, MVP 阶段)
- **SQLite 单文件**: 不用 Postgres / Redis
- **单进程 FastAPI**: uvicorn 1 worker, 无 HA
- **WS event bus 缓后**: MVP 先做锁 + 设备, event bus / actor identity 进 Phase 2
- **MVP 缩到 2 能力**, 估时 ~1.5 人天 (vs R3 的 3.5)

## 3. MVP API (Phase 1, 必做)

### 3.1 设备注册 + 心跳

```http
POST /devices/register
Body: { serial, device_type ("physical"|"cloud:*"), owner_actor ("a"|"b"),
        capabilities[], heartbeat_ttl_seconds, meta{} }
→ 200 { device_id, registered_at }
→ 409 { conflict_with_actor } (同 serial 双注册时)

POST /devices/{device_id}/heartbeat
→ 200 { ok, expires_at }

GET /devices?owner=a&status=online
→ [{...}]
```

### 3.2 跨 repo 锁

```http
POST /locks/acquire
Body: { resource, actor, ttl_seconds, wait_max_seconds }
→ 200 { lock_id, expires_at }
→ 409 { held_by_actor, held_until }

POST /locks/{lock_id}/release  → 200 { ok }
POST /locks/{lock_id}/refresh  Body: { ttl_seconds } → 200 { expires_at }
```

**资源命名硬契约**:
- `device:{serial}:messenger_app` — Messenger App 前台 (A fallback / B inbox / B runner 都要)
- `device:{serial}:fb_app` — FB App 前台 (A 独占)
- `device:{serial}:adb` — adb shell 串行
- `peer:{canonical_id}:chat` — 同 lead 对话权 (Phase 2)

## 4. Phase 2 API (验证 MVP 后做)

- **Event bus (WebSocket)**: TG R3 §3.2.C, topic `device.*` / `lock.*` / `greeting.*` / `messenger.reply.*` / `handoff.*` / `contact.merged`
- **Actor 身份注册**: TG R3 §3.2.D, MVP 阶段静态 `{a, b}` 硬编码即可

## 5. SQLite Schema

```sql
CREATE TABLE devices (
  device_id TEXT PRIMARY KEY,
  serial TEXT UNIQUE NOT NULL,
  device_type TEXT NOT NULL,
  owner_actor TEXT NOT NULL,
  capabilities_json TEXT,
  meta_json TEXT,
  heartbeat_ttl_seconds INTEGER NOT NULL,
  last_heartbeat_at TEXT,
  registered_at TEXT NOT NULL
);
CREATE TABLE locks (
  lock_id TEXT PRIMARY KEY,
  resource TEXT NOT NULL,
  actor TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  released_at TEXT
);
CREATE INDEX idx_locks_resource_active ON locks(resource) WHERE released_at IS NULL;
```

## 6. A 侧 client SDK 接入 (sibling 实施)

### 6.1 fb_concurrency.py 加 backend 抽象

```python
# src/host/fb_concurrency.py
import os
from contextlib import contextmanager

LOCK_BACKEND = os.environ.get("LOCK_BACKEND", "local")  # local | coordinator

@contextmanager
def device_section_lock(device_id, section, *, timeout=60.0):
    if LOCK_BACKEND == "coordinator":
        with _coord_lock(device_id, section, timeout) as lock_id:
            yield lock_id
    else:
        with _local_lock(device_id, section, timeout):  # 原 threading.Lock 保留
            yield None
```

### 6.2 新 `src/host/coord_client.py`

- httpx wrapper, ~80 行
- `POST /locks/acquire` + heartbeat 后台线程 + release on context exit
- **Coordinator 不可达自动 fallback 本地 threading.Lock** 保业务可用 (graceful degrade)

## 7. B 侧 client SDK (B 实施)

类似 6.2, 在 `telegram-mtproto-ai/src/integrations/coord_client.py`. B 跑 messenger_rpa runner 前 acquire `device:{serial}:messenger_app`.

## 8. 部署

```bash
mkdir D:\workspace\coordinator
cd D:\workspace\coordinator
python -m venv .venv && .venv\Scripts\activate
pip install fastapi uvicorn sqlalchemy httpx
# 写 main.py + db.py
uvicorn main:app --host 127.0.0.1 --port 9810
```

启动后 A/B 设 `LOCK_BACKEND=coordinator` 环境变量启用 (默认 `local` fallback).

## 9. 测试策略

- **单测**: pytest mock coord client, 验 acquire/release/timeout/fallback graceful degrade
- **集成**: 起本机 coordinator + 两 repo runner, mock 抢锁
- **真机**: bg_phone_1 跑 A facebook 任务 + B messenger 任务, 验证序列化无 ADB 撞车

## 10. 路径

- 部署根: `D:\workspace\coordinator\` (独立 .venv, 不在任何 repo)
- spec (本文件): `D:\workspace\mobile-auto0423\docs\COORDINATOR_SPEC.md`
- A client: `D:\workspace\mobile-auto0423\src\host\coord_client.py` (sibling 写)
- B client: `D:\workspace\telegram-mtproto-ai\src\integrations\coord_client.py` (B 写)

## 11. 实施顺序 (sibling 接手)

1. 起 `D:\workspace\coordinator\` FastAPI 骨架 (`main.py` + `db.py`, ~150 行)
2. 实施 Phase 1 API (设备 + 锁), 单测
3. A 侧 `fb_concurrency.py` 加 backend 抽象 + `coord_client.py`
4. 真机验证 (bg_phone_1 跑 A + B 序列化)
5. 通知 B 实施 client SDK (B 看本 spec + 自家 R3 doc)
6. 上线后再加 Phase 2 API (event bus + actor)

## 12. 与 R3 原 spec 的差别 (供 B 参考)

- 没有 cross-host (Tailscale 等), localhost only — 因为同机
- 没有 actor API key auth — MVP 阶段无必要
- Event bus / actor identity 推到 Phase 2 — 不阻塞核心锁能力
- 客户端 fallback local threading.Lock — coordinator 挂掉业务不停

— A-main (2026-04-25, 由 A-sibling 接手实施)
