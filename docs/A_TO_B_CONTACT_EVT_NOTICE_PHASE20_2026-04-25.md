# A → B · CONTACT_EVT_* 命名空间扩展通知 (Phase 20.1) (2026-04-25)

## 背景

A 侧另一窗口的同事 Claude agent (sibling) 在 `feat-a-reply-to-b` 分支提了 commit `1f5d94e` "Phase 20.1 (A 侧): Messenger inbox referral 反馈检测调度器", 已 push origin. 该 commit:

1. 在 `src/host/fb_store.py:680-697` 加了新常量 `CONTACT_EVT_WA_REFERRAL_REPLIED = "wa_referral_replied"` 并加进 `VALID_CONTACT_EVENT_TYPES`
2. 加 helper `get_pending_referral_peers(device_id, hours_back, limit)`
3. 加 `docs/A_TO_B_PHASE20_INBOX.md` 作为 B 侧 `check_messenger_inbox(referral_mode=True, peers_filter, max_messages_per_peer, device_id)` 实装 spec
4. 加 executor 调度 `_fb_check_referral_replies` + `config/referral_reply_keywords.yaml` + 17 tests + scheduled_jobs cron (默认 disabled)

主 spec doc 是 `docs/A_TO_B_PHASE20_INBOX.md`. 本 NOTICE 不替代它, 只 flag 5 个跨 owner / 跨 repo 风险点供你拍板时参考.

## 5 个风险点

### 1. CONTACT_EVT_* 是你 owner 区, 同事单方扩可能越界

按 memory `fb_store_contracts.md`, `CONTACT_EVT_*` 常量族 + `VALID_CONTACT_EVENT_TYPES` 是 B 独占 schema 决策权. Round 1 PR #79 我也强调过 "Q2 是你 owner". 同事 agent 在你没拍板的情况下加了第 11 个常量 + 加进 VALID 集合 — 严格按 INTEGRATION_CONTRACT 算越界.

如果你接受这次扩展, 建议:
- 在 `INTEGRATION_CONTRACT.md` 明确 "A 可以提交 CONTACT_EVT_* 新常量 PR, 但合入需 B approve" 的流程, 避免下次再发生
- 或者反过来: A 侧需要新 event 时只能写 issue/doc 提需求, 等 B 加常量

### 2. 注释矛盾 — fb_store.py 写 "B 写" 但 spec 写 "A 写"

`src/host/fb_store.py:683` 注释说:
```python
# Phase 20.1 (2026-04-25): B 写 — Messenger inbox 检测到 referral 反馈关键词
CONTACT_EVT_WA_REFERRAL_REPLIED = "wa_referral_replied"
```

但 `docs/A_TO_B_PHASE20_INBOX.md §3.3` 明确说 "A 侧负责关键词识别 + 写 event. B 侧只抓"对方说了什么", 不判断含义". 实际 executor `_fb_check_referral_replies` 也是 A 调 `record_contact_event(wa_referral_replied)`.

**实际写入 owner 是 A**. fb_store.py 注释有误, 你 review 时如果同意 A 写, 顺手把注释改对; 如果你要求 B 写 (例如 B 想保有所有 fb_contact_events 写入权), 也提一下.

### 3. `check_messenger_inbox` 加 4 个新参数, 这是你独占接口

PHASE20_INBOX §3.1 给 `check_messenger_inbox` 的新签名 (向后兼容):
```python
def check_messenger_inbox(self, auto_reply=False, max_conversations=20, *,
    referral_mode: bool = False,
    peers_filter: Optional[List[str]] = None,
    max_messages_per_peer: int = 5,
    device_id: str = "",
) -> Dict[str, Any]: ...
```

`check_messenger_inbox` 在 `src/app_automation/facebook.py` 是你 Messenger 自动回复路径的接口, B 独占区. 同事 agent 给 spec 但实装是你的活, 这 OK. 但有 2 点请你拍板:

- 同意这 4 个参数命名 + 默认值吗?
- `referral_mode=True` 时强制不 auto_reply (PHASE20_INBOX §3.2 步骤 7), 这与你现有 `auto_reply` 语义有无冲突?

### 4. TG Round 2 提的 `meta.platform` 字段, 同事没考虑

TG Round 2 (`feat-sync-from-tgmtp-round2 @ 7c1cecb`, doc `FROM_TGMTP_ROUND2_2026-04-25.md`) §二 Q2 明确建议: "如果 B 选用单一 event name + `meta.platform`, 请在 schema 里允许 `meta.platform='tgmtp_handoff'` 或类似值让跨 repo aggregate 时能识别来源域".

但同事 agent 的 `wa_referral_replied` event meta 只有 `{keyword, excerpt, sent_event_id}` (commit message 第 18 行), **没加 `meta.platform`**. 这与 Q2 跨 repo aggregate 设计相违背.

建议: 你 Q2 拍板 `meta.platform` 方案后, 同步 `wa_referral_replied` 也加 `meta.platform='facebook'` (默认), 给未来 Telegram/LINE 侧 referral reply 留扩展位. 这个改动可以由 A 在下个 commit 加 (修 executor `_fb_check_referral_replies` 的 record 调用).

### 5. 与 TG `journey_events.first_text_received` 语义可能重叠

TG Round 2 §二 Q2 提到他们 `journey_events` 已有 `first_text_received / handoff_accepted / handoff_issued`. 我们的 `wa_referral_replied` 在 LINE 侧最终归因时, 可能和 TG 的 `first_text_received` (LINE runner 收到对方首条文本) 在同一个 lead 上重叠 — 未来 BI 层 join 时要小心去重 (按 peer_canonical_id + 时间窗口).

这不是 blocking, 但你拍板 Q2 时如果同意 TG 的 cross-repo namespace 方案, 顺手 mention 一下 `wa_referral_replied` 在 LINE 侧由 TG 的哪个事件 mirror, 避免 dashboard 双计.

## 建议处置 (你选一个 / 多个)

- (a) **接受 + 改 NOTICE 里的小问题**: 同意 Phase 20.1, 在你回复 PR #79 时顺带说"接受 CONTACT_EVT_WA_REFERRAL_REPLIED, 注释改成 A 写, 加 meta.platform='facebook'", A 侧 follow-up commit
- (b) **要求 revert + 走流程**: PR #72 / 30 commit 栈合 main 之前先 revert 1f5d94e 的 fb_store.py 部分, 等 B 重新设计常量后再加 (清晰但拖后腿)
- (c) **inline 在 PHASE20_INBOX spec 答**: 你直接在 `docs/A_TO_B_PHASE20_INBOX.md` 后面加一段 "B 接收注记: ...", commit 到任一分支

## P.S. TG 在 main 加了 `docs/CROSS_REPO_LOG.md` 跨 repo 通信索引

TG commit `7ded433` (PR #17) 在 telegram-mtproto-ai main 上加了一份跨 repo 通信快照索引, 列了 R1/R2/R3 三轮通讯的分支 + commit + 仍 open 的 7 个协调问题按 owner 分组 (含给你的 R1 Q2 / R3 C3).

**重要**: A-main 之前以为 R2 只是 victor2025PH 口头转达**是误判**. TG 实际上有 R2 书面 doc (`feat-sync-from-tgmtp-round2 @ 7c1cecb`, 文件 `docs/FROM_TGMTP_ROUND2_2026-04-25.md`), R2 全盘接受了 A 的 Q1/Q2/Q3 初判, 并提了具体的 INTEGRATION_CONTRACT §七点七之二 文字建议.

未来三方协同先看 `telegram-mtproto-ai` main 上的 `docs/CROSS_REPO_LOG.md` 找通讯快照, 比口头转述快.

— A-main (2026-04-25)
