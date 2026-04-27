"""Phase 2 P1 后端 normalize — error_classifier unit tests.

覆盖:
- 6+ 类规则各自正确归类
- 顺序优先级 (具体 → 一般): SLA 比 plain timeout 优先, group_already_joined
  比 vision_join_button_miss 优先 (即"已加入"显示前优先匹配为 success-skip)
- 边界: None / 空字符串 / 全空白 → None
- 无规则匹配 → layer='unknown' + 截断 60 字
- 大小写不敏感 (re.I)
"""
import pytest

from src.host.error_classifier import classify_task_error


@pytest.mark.parametrize("raw, layer, code", [
    # quota
    ("facebook_join_group quota exceeded for []: 3/3 in hourly window",
     "quota", "rate_limited"),
    ("Hourly window QUOTA EXCEEDED",
     "quota", "rate_limited"),
    # infra
    ("[gate] 预检未通过 (network): 无法访问外网",
     "infra", "vpn_no_ip"),
    ("无法访问外网 (代理失败)",
     "infra", "vpn_no_ip"),
    ("adb offline: device 4HUSIB4T not found",
     "infra", "adb_offline"),
    ("ADB device 4HUSIB4T not online after 30s",
     "infra", "adb_offline"),
    # business — group_already_joined 先于 vision_join_button_miss
    ("group already in group, skip join",
     "business", "group_already_joined"),
    ("用户已加入该群, 无需 join",
     "business", "group_already_joined"),
    # business — group_not_found
    ("group not found: 婚活アラフォー",
     "business", "group_not_found"),
    ("群不存在或已被封",
     "business", "group_not_found"),
    # business — vision_join_button_miss
    ("加入群组失败: smart_tap miss after 3 retries",
     "business", "vision_join_button_miss"),
    ("join_group failed at step 4",
     "business", "vision_join_button_miss"),
    ("Vision 无法找到 Join 按钮",
     "business", "vision_join_button_miss"),
    # business — vision_search_bar_miss
    ("找不到搜索框 (Search bar)",
     "business", "vision_search_bar_miss"),
    ("search bar miss after 5s",
     "business", "vision_search_bar_miss"),
    # safety — circuit
    ("circuit breaker OPEN for device 4HUSIB4T",
     "safety", "circuit_breaker"),
    ("router 熔断中, 拒绝派单",
     "safety", "circuit_breaker"),
    # timing — SLA 优先于 plain timeout
    ("SLA timeout: 30min 无业务事件入库",
     "timing", "sla_timeout"),
    ("SLA abort triggered",
     "timing", "sla_timeout"),
    ("30min 无业务事件 → SLA",
     "timing", "sla_timeout"),
    # timing — plain timeout
    ("operation timeout after 60s",
     "timing", "task_timeout"),
    ("Vision 超时",
     "timing", "task_timeout"),
])
def test_known_patterns(raw, layer, code):
    out = classify_task_error(raw)
    assert out is not None, f"should match: {raw!r}"
    assert out["layer"] == layer, f"{raw!r}: got layer={out['layer']}, want {layer}"
    assert out["code"] == code, f"{raw!r}: got code={out['code']}, want {code}"
    # 通用 shape 校验
    assert out["msg"], "msg 不应为空"
    assert out["tone"] in ("red", "amber", "green"), f"bad tone: {out['tone']}"
    assert out["emoji"] and len(out["emoji"]) <= 4, f"bad emoji: {out['emoji']!r}"


def test_priority_sla_before_timeout():
    """SLA timeout 必须比 plain "timeout" 优先匹配."""
    out = classify_task_error("SLA timeout after 30min")
    assert out["code"] == "sla_timeout", "SLA 应胜过 plain timeout"


def test_priority_already_joined_before_vision_miss():
    """'已加入' 应胜过 '加入群组失败' (avoid 把 success-skip 误归为 vision miss)."""
    # 同一字符串包含两个关键词的情况: 已加入 优先
    out = classify_task_error("用户已加入, 加入群组失败的兜底未触发")
    assert out["code"] == "group_already_joined"


def test_priority_quota_before_anything():
    """quota exceeded 是最具体的, 必须最先匹配."""
    out = classify_task_error("facebook_join_group quota exceeded - 加入群组失败 fallback")
    assert out["code"] == "rate_limited"


@pytest.mark.parametrize("empty", [None, "", "   ", "\n\t\n"])
def test_empty_returns_none(empty):
    assert classify_task_error(empty) is None


def test_unknown_pattern_truncates():
    """无匹配时归 unknown, msg 截断 60 字."""
    long_text = "some completely unknown error message " * 5  # ~190 字
    out = classify_task_error(long_text)
    assert out is not None
    assert out["layer"] == "unknown"
    assert out["code"] == "unclassified"
    assert len(out["msg"]) <= 61, f"msg should be truncated to ≤60+ellipsis, got {len(out['msg'])}"
    assert out["msg"].endswith("…"), "long msg 应该末尾带省略号"


def test_unknown_short_no_truncate():
    """短的未知错误不加省略号."""
    short = "weird short error"
    out = classify_task_error(short)
    assert out["layer"] == "unknown"
    assert out["msg"] == short
    assert not out["msg"].endswith("…")


def test_returned_shape_keys():
    """所有规则返回的 dict 必须有相同 5 个 key (前端依赖)."""
    out = classify_task_error("quota exceeded test")
    assert set(out.keys()) == {"layer", "code", "msg", "tone", "emoji"}
