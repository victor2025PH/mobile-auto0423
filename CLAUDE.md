# mobile-auto0423 — Claude Code 项目指令

> 本仓库 = **Facebook / Messenger 移动端自动化 bot**，双 worker 同 repo 协作。
> 本文件是 Claude Code 在本 cwd 下自动读取的项目级 memory，所有 Claude session（A/B）都会加载。

## 仓库定位

- **A worker**（greeting bot）：`add_friend` / `send_greeting` / `extract_members` / `browse_feed`
- **B worker**（messenger chat bot）：`check_messenger_inbox` / `_ai_reply_and_send` / `send_message` / `check_message_requests`
- 两者在同一 repo、共享 `src/app_automation/facebook.py` / `src/host/fb_store.py` / `src/host/database.py`，按契约分工

**硬边界、数据库列语义、task_type 命名空间、gate 注册表、device_section_lock 命名空间**
→ 全部看 [`docs/INTEGRATION_CONTRACT.md`](docs/INTEGRATION_CONTRACT.md)（权威契约，任何跨边界修改必须先改本文件再改代码）。

## 明确不在本 repo 范围（防混淆）

| 内容 | 实际归属 |
|---|---|
| contacts / handoff 跨平台 Contact / HandoffToken 子系统 | `telegram-mtproto-ai` repo（另一个项目） |
| Telegram / LINE RPA runner | `telegram-mtproto-ai` |
| Android Messenger RPA runner（adb + UIAutomator 驱动手机里的 Messenger App） | `telegram-mtproto-ai/src/integrations/messenger_rpa/` |
| skill_manager / KB / trigger / 回复生成的主体 | `telegram-mtproto-ai` |

本 repo 的 "Messenger" 指 **Facebook Messenger 通过 FB App / Messenger App 的 UI 自动化**（`facebook.py::send_message` / `check_messenger_inbox` 等），走的是 mobile-auto0423 自己的 VLM Level 4 fallback 栈，**与 telegram-mtproto-ai 的 messenger_rpa runner 完全独立**（两套实现，不共享代码）。

## Claude 协同约定（A/B 通用）

- **身份**：本 repo 双 worker 协作（A = greeting bot，B = messenger chat bot）；两 Claude 共用 `victor2025PH` token，身份由**物理机器 + `INTEGRATION_CONTRACT.md §二` 的独占区**决定，**不**由本文件自动 assign。进入本 repo 前先确认自己改的文件归谁。
- **Git workflow**：`feat-a-*` / `feat-b-*` 分支 → PR → approve-equivalent review → squash merge。不直推 main。
- **approve-equivalent 模式**：共用 token 导致 GitHub 拒绝 author `APPROVED`。用 `COMMENTED` + marker `"✅ A 侧 review 通过 (approve-equivalent)"` 代替；`auto_merge_stack.py` / `check_a_activity.py` 识别该 marker 放行。
- **共享 Claude 记忆**：`~/.claude/projects/C--telegram-mtproto-ai/memory/` 里 `MEMORY.md` 按项目分组（同一 Claude 账号轮流在 A/B repo 工作，单点记忆库）。

## 崩溃恢复

Claude 崩溃后重入本 repo：

1. `git fetch origin && git checkout main && git pull`
2. `gh pr list --author victor2025PH --state open` 看自己还有哪些 PR 在开
3. 读 `docs/B_RESUME_2026-04-23-EVENING.md` 的 Step 1-9（手动恢复脚本）
4. 读 `~/.claude/projects/C--telegram-mtproto-ai/memory/MEMORY.md` 的 "Project: mobile-auto0423" 段，重建上下文

## 日常测试

```bash
python -m pytest tests/ -x -q --ignore=tests/e2e -k "not real"
```

commit 前必跑。跨边界改 PR 必须 @对方 review。
