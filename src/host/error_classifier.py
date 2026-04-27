"""任务失败原因归类 — Phase 2 P1 后端 normalize.

把 raw `last_error` 文本映射到稳定的 (layer, code, msg, tone, emoji) 五元组,
让前端不再用脆弱的 regex 匹配, 跨边界 (sibling 改业务方法时) 也不会破.

## 业务方约定 (跨边界 contract — 修改本文件需 @对方 review)

业务方法 raise / 写入 last_error 时, **请优先用以下文本片段** 让分类引擎能识别:

| Layer | Code | 文本片段示例 (regex 见下方 _RULES) |
|-------|------|-----------------------------------|
| infra | vpn_no_ip | "无法访问外网" / "[gate] 预检未通过 (network)" |
| infra | adb_offline | "adb offline" / "device not found" |
| quota | rate_limited | "quota exceeded" / "in hourly window" |
| business | vision_join_button_miss | "加入群组失败" / "join_group ... fail" / "无法找到 ... Join" |
| business | vision_search_bar_miss | "找不到搜索框" / "search bar miss" |
| business | group_already_joined | "已加入" / "already in group" |
| business | group_not_found | "群不存在" / "group not found" |
| timing | sla_timeout | "SLA timeout" / "30min 无业务" |
| timing | task_timeout | "timeout" / "超时" |
| safety | circuit_breaker | "circuit ... open" / "熔断" |

新加 layer/code 时:
1. 在 `_RULES` 顶部加新规则 (顺序: 最具体 → 最一般)
2. 更新本 docstring 表格
3. 更新前端 fallback `tasks-chat.js::_attributeError` (本类挂时的兜底)
4. 加 unit test 覆盖

## API

```python
from src.host.error_classifier import classify_task_error

cls = classify_task_error("facebook_join_group quota exceeded for []: 3/3")
# {'layer': 'quota', 'code': 'rate_limited',
#  'msg': '配额到 (等下个 window)', 'tone': 'amber', 'emoji': '⏰'}

classify_task_error("")     # → None
classify_task_error(None)   # → None
```
"""
from __future__ import annotations

import re
from typing import Optional


# 规则顺序: 最具体 → 最一般. 第一个匹配胜出.
# 每条 (regex, layer, code, msg, tone, emoji)
_RULES: list[tuple[re.Pattern, str, str, str, str, str]] = [
    # ── quota / rate limit (最具体先) ──
    (re.compile(r"quota exceeded|in hourly window", re.I),
     "quota", "rate_limited", "配额到 (等下个 window)", "amber", "⏰"),

    # ── infra ──
    (re.compile(r"无法访问外网|\[gate\]\s*预检未通过.*network", re.I),
     "infra", "vpn_no_ip", "VPN 不通 (修网络再派)", "red", "🔌"),
    (re.compile(r"adb\s+offline|device not found|adb.*not.*online", re.I),
     "infra", "adb_offline", "设备 adb 离线", "red", "📵"),

    # ── business (具体 vision/group 错误优先) ──
    (re.compile(r"已加入|already in group|already a member", re.I),
     "business", "group_already_joined", "群已加入 (跳过)", "amber", "✅"),
    (re.compile(r"群不存在|group not found|no such group", re.I),
     "business", "group_not_found", "群不存在 / 已被封", "red", "🚫"),
    (re.compile(r"加入群组失败|join_group.*fail|无法找到.*Join", re.I),
     "business", "vision_join_button_miss",
     "Vision 找不到 Join 按钮 (重试可能修复)", "red", "❌"),
    (re.compile(r"找不到搜索框|search bar miss|search bar not found", re.I),
     "business", "vision_search_bar_miss",
     "Vision 找不到搜索框 (FB 可能误入 Messenger)", "red", "🔎"),

    # ── safety (熔断) ──
    (re.compile(r"circuit.*open|熔断", re.I),
     "safety", "circuit_breaker", "熔断保护 (设备/路由器异常)", "amber", "🚧"),

    # ── timing (SLA 比 plain timeout 具体) ──
    (re.compile(r"SLA.*(timeout|abort)|30min.*无业务", re.I),
     "timing", "sla_timeout", "SLA 超时 (30min 无业务事件)", "amber", "⏱️"),
    (re.compile(r"timeout|超时", re.I),
     "timing", "task_timeout", "超时", "amber", "⏳"),
]


def classify_task_error(text: Optional[str]) -> Optional[dict]:
    """归类 last_error 文本到 (layer, code, msg, tone, emoji).

    返回 dict 字段:
    - layer: 'infra' | 'quota' | 'business' | 'safety' | 'timing' | 'unknown'
    - code: 稳定的标识符 (前端 i18n / 聚合分组 key)
    - msg: 用户可读的中文短句
    - tone: 'red' | 'amber' | 'green' (UI 色调)
    - emoji: 1 字符视觉锚 (跟前端 _attributeError 对齐)

    None / 空字符串 / 全空白 → 返 None (调用方按需省略字段).
    无规则匹配 → layer='unknown', code='unclassified', msg=截断 60 字.
    """
    if not text or not str(text).strip():
        return None
    t = str(text)
    for pat, layer, code, msg, tone, emoji in _RULES:
        if pat.search(t):
            return {
                "layer": layer,
                "code": code,
                "msg": msg,
                "tone": tone,
                "emoji": emoji,
            }
    return {
        "layer": "unknown",
        "code": "unclassified",
        "msg": t[:60] + ("…" if len(t) > 60 else ""),
        "tone": "red",
        "emoji": "⚠️",
    }
