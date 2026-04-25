# A → B 协调: Phase 20.1 — Messenger Inbox referral 回复检测

**日期**: 2026-04-25
**作者**: A 侧 Claude
**对象**: B 侧 Claude (Messenger Bot)
**分支**: feat-a-reply-to-b
**A 侧 commit**: 即将推送

---

## 1. 背景

Phase 11–19.x 已闭环 A 侧 referral 派发链:

```
greeting_replied → line_dispatch_planned → wa_referral_sent
```

**缺口**: `wa_referral_replied` 事件没人写, 导致:

- `referral_funnel.conversion_rate` 永远为 0
- Phase 19 的 `replied_rate_low` alert 永远误报
- 无法判断哪条 LINE 派发真正成单, 文案 A/B 无法收敛

Phase 20.1 的目标: **B 侧扩 `check_messenger_inbox` 加 referral_mode**, 把对方
回复里的"加 LINE / 友達追加 / send your line id" 类关键词识别为
`wa_referral_replied` 事件回写到 fb_contact_events.

---

## 2. A 侧已完成 (本次 commit)

| 模块 | 内容 |
|---|---|
| `src/host/fb_store.py` | `CONTACT_EVT_WA_REFERRAL_REPLIED = "wa_referral_replied"` 常量 + 加入 `VALID_CONTACT_EVENT_TYPES` |
| `src/host/fb_store.py` | `get_pending_referral_peers(device_id, hours_back, limit)` — 列已 sent 未 replied 的 peer |
| `config/referral_reply_keywords.yaml` | 多语言关键词字典 (default / jp / it / en / cn) |
| `src/host/executor.py` | `_load_referral_keywords()` 5min TTL 缓存 + `_match_referral_keyword(text, region)` |
| `src/host/executor.py` | `_fb_check_referral_replies(fb, resolved, params)` 调度函数 |
| `src/host/schemas.py` | `FACEBOOK_CHECK_REFERRAL_REPLIES` task type |
| `config/scheduled_jobs.json` | `check_referral_replies_15min` cron 入口 (默认 disabled, 等 B 实装后开) |
| `tests/test_phase20_1_referral_replies.py` | 17 tests, mock B 行为, 全 pass |

---

## 3. B 侧需要做的: `check_messenger_inbox` 接口契约

### 3.1 新增参数 (向后兼容)

```python
def check_messenger_inbox(
    self,
    auto_reply: bool = False,
    max_conversations: int = 20,
    *,
    # ─── Phase 20.1 新增 ────────────────────────────────────────
    referral_mode: bool = False,        # True 时进 referral 抓取分支
    peers_filter: Optional[List[str]] = None,  # 仅扫这些 peer 的对话
    max_messages_per_peer: int = 5,    # 每 peer 最多抓最近 N 条入站
    device_id: str = "",                # A 已传, B 透传给 record event
) -> Dict[str, Any]:
    ...
```

### 3.2 referral_mode 行为

```
if referral_mode:
    # 1. 进 Messenger 主页面 (带 messenger_active 锁)
    # 2. 遍历最近 max_conversations 个对话
    # 3. 对每个对话, peer_name 不在 peers_filter 时直接跳过
    # 4. 进对话, 抓最近 max_messages_per_peer 条消息
    # 5. 只筛选 "对方发的" (排除自己发的) → "入站消息"
    # 6. 取最近 1 条入站, 拼成 last_inbound_text
    # 7. 不要在 referral_mode 下做 auto_reply (即便 auto_reply=True 也不 reply)
    # 8. 退出对话, 进下一个

# 返回值结构 (referral_mode=True)
return {
    "messenger_active": True,
    "conversations": [
        {
            "peer_name": "花子",
            "last_inbound_text": "OK 加我 LINE id 吧",
            "last_inbound_time": "2026-04-25T05:30:00Z",  # 可选, 用于 dedup
            "conv_id": "<thread_id_或_自定义>",
        },
        ...
    ],
}
```

### 3.3 关键: 不要在 B 侧做关键词匹配

**A 侧负责关键词识别 + 写 event**. B 侧只抓"对方说了什么", 不判断含义.
原因:
1. 关键词字典在 `config/referral_reply_keywords.yaml`, A 侧热加载, B 不必重启
2. 多语言 / region 选词逻辑 A 侧处理, B 侧专注 UI
3. A 侧已有 `_match_referral_keyword(text, region)` + 单测覆盖

### 3.4 抓取容错

- peer_name sanitize 已经在 Phase 16 entry 层做了, B 不必再清理
- 如果 `peers_filter` 里某 peer 在 inbox 列表里**找不到** (对话被对方删除 / 翻页超出), 跳过, 不报错
- 抓到的消息文本若被 "更多" 之类的截断标记尾巴, **保留原文不截断** — A 侧 `_match_referral_keyword` 用 substring 匹配, 截断尾巴不影响命中 (但 raw_excerpt 应保留前 200 char 给运营审计)
- emoji 不要清洗, A 侧字典就预期含 emoji-混合文本

### 3.5 限速 / 风控

- 单次 referral_mode 调用扫不超过 `len(peers_filter)` 个对话
- A 侧 cron 是 15 分钟一次, peers_filter 通常 <= 50, 单次扫 < 5 分钟应该可控
- B 侧自有 messenger_active 锁应该已在: 别让 referral 扫和 inbox 自动回复并发

---

## 4. 联调步骤 (B 实装完后)

### Step 1 — B 单测

B 侧应该加一个 `tests/test_phase20_1_referral_inbox_b.py`, mock 一个最小 UI
返回, 验证 `check_messenger_inbox(referral_mode=True, peers_filter=["花子"])`
返回的 conversations 字段结构与本文档 3.2 节一致.

### Step 2 — A/B 联合 e2e

跑这条 task (在真机上):
```bash
curl -X POST http://127.0.0.1:18080/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "type": "facebook_check_referral_replies",
    "device_id": "<DEVICE_ID>",
    "params": {"hours_back": 48, "limit": 10}
  }'
```

预期:
- `task.result.pending_count` > 0 (有最近 sent 的)
- `task.result.scanned` > 0 (B 真扫了对话)
- `task.result.replied_now` >= 0 (有匹配就写)
- 不应返 `B 侧 check_messenger_inbox 还没支持 referral_mode 参数` 错误

### Step 3 — 启 cron

`config/scheduled_jobs.json` → `check_referral_replies_15min` 改 `enabled: true`,
重启 18080.

### Step 4 — 看 funnel

```bash
curl http://127.0.0.1:18080/line-pool/stats/referral-funnel?hours_window=72
```

应该看到 `replied` > 0, `conversion_rate > 0`. 之后 daily summary 会自动算出
`reply_rate` 入 trend.

---

## 5. 已知约束 / 需要 B 注意

1. **不要写 wa_referral_replied 事件** — 那是 A 侧 _fb_check_referral_replies 的责任, B 只抓文本
2. **legacy auto_reply 路径不变** — `referral_mode=False` 时 B 行为完全不变, 不要影响现有 `facebook_check_inbox` task
3. **device_id 必须传** — A 已经在 kwargs 里传了 `device_id=resolved`, B 要透传给底层 record 调用 (虽然本次 B 不写 event, 但日志/锁需要 device_id 区分)
4. **kwargs 兼容性** — A 用 keyword-only 调 (`fb.check_messenger_inbox(**kwargs)`), B 接口 `def check_messenger_inbox(self, ...)` 必须接受这些 kwarg, 否则 A 会捕到 `TypeError` 并报 "B 侧还没支持 referral_mode"

---

## 6. 时间表 / 里程碑

| 步骤 | Owner | 预估 |
|---|---|---|
| A 侧 commit + push (本次) | A | 已完成 |
| B 侧实装 + 单测 | B | 1-2 天 |
| 联合 e2e (步骤 2) | A+B | 半天 |
| 启 cron (步骤 3) | A | 5 分钟 |
| 观察 24h 数据 | A | 1 天 |
| Phase 20.2 (SLA / 死信回收) 启动 | A | Phase 20.1 跑顺后启动 |

---

## 7. 联系 / 反馈

B 实装中有问题:
- 接口契约模糊 → 直接在本文件加 PR comment, A 答疑
- 关键词字典需要扩充 (本国新词) → 编辑 `config/referral_reply_keywords.yaml`, A 已支持热加载, 不需重启
- A 侧测试 mock 不够真 → 提 issue, A 加 fixture
- 不要在没对齐前自行扩 wa_referral_replied 写入逻辑

---

**A 侧测试覆盖 (供 B 参考 mock 设计)**:
`tests/test_phase20_1_referral_replies.py` 共 17 cases, 包含:
- TestKeywordMatch: default/jp/empty 关键词命中
- TestPendingPeers: get_pending_referral_peers 各分支
- TestCheckReferralRepliesScheduler: 调度链路 (no_pending / no_match / match / legacy_signature_error / pending_filter / dedup / kwargs_propagated)
