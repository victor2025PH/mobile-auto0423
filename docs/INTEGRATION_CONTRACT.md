# 双机协同对接契约

> 两台电脑，两个 Claude，一个 monorepo。
> **本文件是唯一的真实边界定义，任何跨边界修改必须先 PR 改本文件再 PR 改代码。**

## 一、协同模型

```
┌─────────────────────────────┐            ┌─────────────────────────────┐
│ 机器 A (victor2025PH)       │            │ 机器 B (另一台 Claude)       │
│ 负责: 加好友 + 打招呼       │  同 repo  │ 负责: Messenger 自动回复    │
│ 分支: main / feat-a-*        │  ←────→   │ 分支: feat-b-chat           │
└─────────────────────────────┘            └─────────────────────────────┘
            ↓                                       ↓
            └───── 共享 SQLite openclaw.db ────────┘
            └───── 共享 config/ 配置文件 ──────────┘
            └───── 共享 src/app_automation/ 核心 ──┘
            └───── 共享 docs/INTEGRATION_CONTRACT.md ┘
```

**两个项目在部署层独立运行**（各自启 server 就跑自己的 task_type），
**在代码层共享**（automation 层、数据库层、配置层、文案库、playbook）。

## 二、职责分区（硬边界）

### 机器 A 负责（greeting bot）

**业务关键词**：搜索名字 / 加好友 / 打招呼 / 提取群成员 / 点名添加

**独占写**（B 不得修改）：
- `src/app_automation/facebook.py` 中以下方法：
  - `add_friend` / `add_friend_with_note` / `add_friend_and_greet`
  - `send_greeting_after_add_friend`
  - `_tap_profile_message_button` / `_confirm_message_request_if_any` / `_set_greet_reason`
  - `extract_group_members` / `group_engage_session` / `search_people` / `search_and_collect_leads`
  - `browse_feed` / `browse_feed_by_interest`
  - `view_profile` / `read_profile_about`
- `src/host/fb_add_friend_gate.py`（全文件）
- `src/host/fb_account_phase.py`（全文件）
- `src/host/fb_playbook.py` 中 `send_greeting` / `add_friend` / `extract_members` / `group_engage` 段
- `config/facebook_playbook.yaml` 中 `send_greeting:` / `add_friend:` / `extract_members:` / `group_engage:` 段
- `src/host/routers/facebook.py` 中 `friend_growth` / `group_hunter` / `name_hunter` / `warmup` / `full_funnel` 预设
- `src/host/static/js/facebook-ops.js` 中 `fbOpenNameHunterInput` 相关代码
- task_type 命名空间：
  - `facebook_add_friend`
  - `facebook_add_friend_and_greet`
  - `facebook_send_greeting`
  - `facebook_extract_members`
  - `facebook_browse_groups`
  - `facebook_group_engage`
  - `facebook_browse_feed`
  - `facebook_browse_feed_by_interest`
  - `facebook_search_leads`
  - `facebook_join_group`
  - `facebook_profile_hunt`

**数据库写入**：
- `facebook_friend_requests` 表 —— A 独占写
- `facebook_groups` 表 —— A 独占写
- `facebook_inbox_messages` 表 —— 写 `direction='outgoing' + ai_decision='greeting'` 专属
  - `peer_type='friend_request'` 表示对方尚未接受好友请求（消息请求文件夹）
  - `template_id` 格式 `<src>:<cc_or_lang>:<idx>`（如 `yaml:jp:3`），带 `|fallback` 后缀表示走 A1 降级路径

### 机器 B 负责（messenger chat bot）

**业务关键词**：Messenger 收件箱 / 自动回复 / 消息请求 / 引流话术切换 / 好友请求审核

**独占写**（A 不得修改）：
- `src/app_automation/facebook.py` 中以下方法：
  - `check_messenger_inbox` / `check_message_requests` / `check_friend_requests_inbox`
  - `_ai_reply_and_send` / `_extract_latest_incoming_message`
  - `_open_message_requests_fallback` / `_list_friend_requests`
  - `send_message`（Messenger 发消息主路径 —— A 的 A2 降级会 *只读调用* 这个方法，但不改其实现）
- `src/ai/auto_reply.py`（如有，全文件）
- `src/ai/chat_brain.py`（如有，全文件）
- `src/host/fb_playbook.py` 中 `check_inbox` 段
- `config/facebook_playbook.yaml` 中 `check_inbox:` 段
- `src/host/routers/facebook.py` 中 `inbox_pro` 预设
- task_type 命名空间：
  - `facebook_check_inbox`
  - `facebook_check_message_requests`
  - `facebook_check_friend_requests`
  - `facebook_send_message`
  - 任何新建的 `facebook_*_reply` / `facebook_*_chat` task_type

**数据库写入**：
- `facebook_inbox_messages` 表 —— 写 `direction='incoming'` 所有行
- `facebook_inbox_messages` 表 —— 写 `direction='outgoing' + ai_decision IN ('reply', 'wa_referral')`
- 允许回写 A 写入的 greeting 行的 `replied_at` 字段（当对方回复了 greeting 时）

### 共享区（双方只读 + 协商修改）

双方都能读，但修改必须先在 PR 里 @对方 review：

- `src/host/fb_store.py` — 数据层函数（`record_friend_request` / `record_inbox_message` / `count_*` / `get_funnel_metrics`）
- `src/host/database.py` — schema 迁移（两边 ALTER TABLE 必须合并到同一个 migration 列表，不能互相覆盖）
- `src/app_automation/fb_content_assets.py` — 文案加载器（`get_verification_note` / `get_greeting_message` / `get_comment_pool` / `get_referral_snippet`）
- `config/chat_messages.yaml` — 文案模板池（多语种开场白、评论、引流话术）
- `config/fb_target_personas.yaml` — 客群定义
- `src/host/executor.py` — task_type 分发器（各自加自己的 case 分支，不碰对方的）
- `src/host/routers/tasks.py` — POST /tasks 的 gate 检查（各自加自己的 gate 调用）
- `src/host/schemas.py` — task_type 枚举
- `src/host/task_labels_zh.py` — 中文标签

## 三、数据库表 —— 字段语义权威定义

### `facebook_inbox_messages`

| 字段 | 写入方 | 语义 |
|------|--------|------|
| `device_id` | 双方 | 设备 ID |
| `peer_name` | 双方 | 对方姓名 |
| `peer_type` | 双方 | `friend`（已是好友） / `friend_request`（刚发好友请求未接受） / `stranger`（message request 里的陌生人） / `group` |
| `direction` | 双方 | `incoming` / `outgoing` |
| `message_text` | 双方 | 消息正文 |
| `ai_decision` | 双方 | `greeting`（A 写：加完好友打招呼） / `reply`（B 写：自动回复） / `wa_referral`（B 写：引流到 WhatsApp/LINE） / `''`（incoming 默认） |
| `ai_reply_text` | 双方 | 出站方向时与 message_text 相同 |
| `language_detected` | B | B 做语言检测时填 |
| `seen_at` | 双方 | 创建时间（incoming = 看到时间；outgoing 也会填但语义弱） |
| `sent_at` | 双方 | **outgoing 专用发送时间** —— `count_outgoing_messages_since` 优先读这个字段 |
| `replied_at` | B | 当 B 回复了一条 incoming 时，同时把被回复的 incoming 行的 `replied_at` 设为回复时间；**也可以回写 A 写入的 greeting 行，标记"对方回复了我们的打招呼"** |
| `template_id` | 双方 | A 写打招呼模板 ID；B 写 reply 模板 ID（如有） |
| `lead_id` | B | 当识别到与 leads.db 的 lead 对应时填 |
| `preset_key` | 双方 | 当前运行的预设 key |

### `facebook_friend_requests`

A 独占写。B 只读。

### `facebook_groups`

A 独占写。B 只读。

### `fb_risk_events`

双方都写（检测到风控时上报）。

## 四、task_type 命名空间

双方在 `src/host/schemas.py::TaskType` 枚举里**追加**自己的 task_type，不修改对方的。

**命名约定**：
- A 的任务：`facebook_<动作>` 不包含 `inbox` / `message` / `reply` / `chat` 关键词
- B 的任务：`facebook_<动作>` 包含 `inbox` / `message` / `reply` / `chat` / `followup` 关键词

**争议处理**：若无法确定归属，写在本文件 `§六 遗留问题` 先挂起，下次碰头再决。

## 五、playbook 配置

`config/facebook_playbook.yaml` 顶层 `defaults:` / `phases:` 结构是**硬约定**。双方在对应的段内增加字段不冲突：

```yaml
defaults:
  add_friend:       # A 维护
  send_greeting:    # A 维护
  extract_members:  # A 维护
  group_engage:     # A 维护
  browse_feed:      # A 维护
  check_inbox:      # B 维护  ← 对方加字段只改这段
  risk:             # 共享 — 改动需 PR review

phases:
  cold_start: {…}   # 双方都要在各自段里设置该 phase 的保守值
  growth: {…}
  mature: {…}
  cooldown: {…}     # 冷却期双方都必须归零
```

`src/host/fb_playbook.py::_PHASE_AWARE_SECTIONS` 也是共享，增加段时双方协商。

## 六、Git workflow

```
main                 ← 稳定分支，双方都能 merge（review 后）
 ├── feat-a-*        ← A 的 feature branch
 └── feat-b-*        ← B 的 feature branch
```

**铁律**：
1. **不直接 push main**，永远走 PR
2. 每个 PR 必须跑完 `pytest tests/ -x` 后再 merge（GitHub Actions 如果接好自动跑）
3. 涉及 "共享区" 文件的 PR 必须 @对方 review
4. 合并策略用 **squash merge** 保持 main 线性
5. 两方 baseline 分歧时，以 main 为准，各自 rebase 自己的分支

**日常循环**：
```bash
git fetch origin
git checkout main && git pull
git checkout -b feat-a-optimize-gate
# ... 改代码、pytest ...
git push -u origin feat-a-optimize-gate
# GitHub 网页上开 PR → review → merge
```

## 七、遗留问题 / 待协商

> 双方遇到不确定归属的事情先写在这里，不要直接动代码。

- [ ] ~~greeting 的 reply_rate 计算：B 在什么时机把对方回复写回 greeting 行？每次 check_inbox 扫一次匹配 `peer_type=friend_request + sent_at 在 7 天内` → 若有新 incoming → 把 greeting 行的 replied_at 设上~~（2026-04-23 A 提议方案，待 B 确认）
- [ ] `fb_contact_events` 表：要不要新建统一事件表记 `(add_friend, greeting, reply_received)` 三元组，修正 daily_cap 模型？
- [ ] 共享的 `ai_cost_events` 表，B 跑 LLM 回复也要写入，格式对齐
- [ ] `facebook_check_inbox` 如果回复的是 greeting 触发的消息，要不要在回复里附上用户的引流 ID？由 B 实现

## 八、历史变更

- 2026-04-23 首版 — 由 A 起草（v1.1.0 + Phase 1/2 实施完成后）
