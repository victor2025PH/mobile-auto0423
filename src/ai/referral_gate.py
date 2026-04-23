# -*- coding: utf-8 -*-
"""引流时机决策闸 (P5 — B 机统一的 wa_referral 决策层)。

把 P3 的 should_block_referral + P4 的 intent-based 硬触发 + ChatBrain 的
ref_score 判断整合到一个可独立测试的模块。_ai_reply_and_send 只调一次
``should_refer`` 拿 GateDecision,再也不用关心分层决策的细节。

决策分 3 层 (按优先级高→低):

  1. **hard_block** (安全优先 — 下列任一命中就不引流):
     * 没有引流目标 (``has_contact=False``)
     * 历史画像显示上次引流对方未回 (P3 ``should_block_referral``),
       且本轮 intent 不是 ``referral_ask`` (对方主动要就放行)
     * 最近 ``refer_cooldown_hours`` 内已引流过,且 intent 不是 ``referral_ask``

  2. **hard_allow** (意图层硬信号 — 对方明确要):
     * ``intent == 'referral_ask'`` → 对方主动问联系方式,必须回
     * ``intent == 'buying'`` → 强购买信号,该引流就引

  3. **soft score** (累加打分,阈值 ``score_threshold`` 判通过):
     * total_turns ≥ min_turns (累积信任度): +1
     * intent == 'interest' (对方表现兴趣): +1
     * ref_score > 0.5 (ChatBrain LLM 判断值得引流): +1
     * lead_score ≥ min_lead_score (A 打分高质量素人): +1
     * peer_reply_count ≥ 2 (对方已多次回复,对话活跃): +1
     分数 ≥ ``score_threshold`` (默认 3) → refer=True,否则 False

**Why 分层而不是纯打分**: hard_block 和 hard_allow 是 "必须遵守的规则",
soft score 是 "综合判断"。把 referral_ask 或 no contact 的 edge case 混进
打分层,边界表达不清晰,也让 gate 不可解释。

**Why intent=referral_ask 覆盖 cooldown**: 对方明确问"你 LINE 几号"时,
用冷却门硬怼回去会让对话出戏 (像真人会说 "我刚才发过了哦")。改用应答 +
重发 ID 是更自然的行为。buying 不覆盖 cooldown: 刚引流对方未回又说要买,
继续引可能是 spam 感知,让 LLM 走 reply 路径回答价格更稳。
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 默认配置
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG: Dict[str, Any] = {
    "min_turns": 3,                # soft: 累积对话轮数门槛
    "min_lead_score": 60,          # soft: A 打分门槛
    "min_peer_replies": 2,         # soft: 对方回复次数门槛
    "score_threshold": 3,          # soft: 5 档评分里满足 3 档即通过
    "refer_cooldown_hours": 1,     # hard: 同 peer 再次引流的冷却期
    "ref_score_threshold": 0.5,    # soft: ChatBrain referral_score 门槛
}


@dataclass
class GateDecision:
    refer: bool
    level: str              # "hard_block" | "hard_allow" | "soft_pass" | "soft_fail"
    score: int = 0          # soft gate 累计分 (level="soft_*" 时有意义)
    threshold: int = 0      # 阈值 (用于日志/telemetry)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "refer": self.refer,
            "level": self.level,
            "score": self.score,
            "threshold": self.threshold,
            "reasons": list(self.reasons),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────

def should_refer(*,
                 intent: str = "smalltalk",
                 ref_score: float = 0.0,
                 memory_ctx: Optional[Dict[str, Any]] = None,
                 lead_score: int = 0,
                 has_contact: bool = False,
                 config: Optional[Dict[str, Any]] = None,
                 now: Optional[_dt.datetime] = None) -> GateDecision:
    """返回引流闸决策 — graceful, 永不抛。

    Args:
        intent: 来自 chat_intent.classify_intent 的 intent tag
        ref_score: ChatBrain 生成 reply 时给的 referral_score (0.0-1.0)
        memory_ctx: 来自 chat_memory.build_context_block 的记忆块,包含:
            - should_block_referral: 上次引流未回的标记
            - profile: { total_turns, peer_reply_count, last_referral_at, ... }
        lead_score: A 机 fb_lead_scorer_v2 落库的 leads.score (0-100)
        has_contact: 是否配置了 referral_contact (WhatsApp/LINE ID)
        config: 覆盖 DEFAULT_CONFIG 部分字段,未指定字段取默认
        now: 注入时钟用于测试 cooldown,默认 utcnow

    Returns:
        GateDecision,调用方 if .refer: decision='wa_referral' else 'reply'
    """
    cfg = dict(DEFAULT_CONFIG)
    if config:
        cfg.update(config)
    memory_ctx = memory_ctx or {}
    profile = memory_ctx.get("profile") or {}
    now = now or _dt.datetime.utcnow()

    reasons: List[str] = []

    # ── 层 1: hard_block ────────────────────────────────────────────────
    if not has_contact:
        reasons.append("无引流目标(has_contact=False)")
        return GateDecision(refer=False, level="hard_block",
                            reasons=reasons)

    # should_block_referral (上次引流对方未回) — referral_ask 除外
    if memory_ctx.get("should_block_referral"):
        if intent != "referral_ask":
            reasons.append("上次引流对方未回复(should_block_referral=True)")
            return GateDecision(refer=False, level="hard_block",
                                reasons=reasons)
        reasons.append("should_block_referral 但 intent=referral_ask,放行")

    # cooldown — referral_ask 除外
    cooldown_hours = int(cfg.get("refer_cooldown_hours", 1))
    if cooldown_hours > 0 and intent != "referral_ask":
        last_ref_iso = profile.get("last_referral_at") or ""
        if last_ref_iso:
            last_ref_dt = _parse_iso(last_ref_iso)
            if last_ref_dt is not None:
                delta = (now - last_ref_dt).total_seconds()
                if 0 <= delta < cooldown_hours * 3600:
                    reasons.append(
                        f"冷却期未过({delta/60:.0f}min < {cooldown_hours}h)"
                    )
                    return GateDecision(refer=False, level="hard_block",
                                        reasons=reasons)

    # ── 层 2: hard_allow ────────────────────────────────────────────────
    if intent == "referral_ask":
        reasons.append("intent=referral_ask (对方主动要联系方式)")
        return GateDecision(refer=True, level="hard_allow",
                            reasons=reasons)
    if intent == "buying":
        reasons.append("intent=buying (强购买信号)")
        return GateDecision(refer=True, level="hard_allow",
                            reasons=reasons)

    # ── 层 3: soft score ────────────────────────────────────────────────
    score = 0
    total_turns = int(profile.get("total_turns", 0) or 0)
    if total_turns >= int(cfg["min_turns"]):
        score += 1
        reasons.append(f"total_turns={total_turns} ≥ {cfg['min_turns']}")

    if intent == "interest":
        score += 1
        reasons.append("intent=interest")

    try:
        if float(ref_score) > float(cfg["ref_score_threshold"]):
            score += 1
            reasons.append(
                f"ref_score={float(ref_score):.2f} > "
                f"{float(cfg['ref_score_threshold']):.2f}"
            )
    except (TypeError, ValueError):
        pass

    if int(lead_score) >= int(cfg["min_lead_score"]):
        score += 1
        reasons.append(f"lead_score={lead_score} ≥ {cfg['min_lead_score']}")

    peer_replies = int(profile.get("peer_reply_count", 0) or 0)
    if peer_replies >= int(cfg["min_peer_replies"]):
        score += 1
        reasons.append(
            f"peer_reply_count={peer_replies} ≥ {cfg['min_peer_replies']}"
        )

    threshold = int(cfg["score_threshold"])
    passed = score >= threshold
    return GateDecision(
        refer=passed,
        level="soft_pass" if passed else "soft_fail",
        score=score,
        threshold=threshold,
        reasons=reasons,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_iso(s: str) -> Optional[_dt.datetime]:
    """兼容 'YYYY-MM-DDTHH:MM:SSZ' 和 'YYYY-MM-DD HH:MM:SS' 两种格式。"""
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    # 尝试 fromisoformat (允许带时区)
    try:
        return _dt.datetime.fromisoformat(s.rstrip("Z"))
    except Exception:
        return None
