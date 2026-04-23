# -*- coding: utf-8 -*-
"""SQLite 持久化层。WAL 模式，支持并发读写。"""

import sqlite3
import logging
from pathlib import Path
from contextlib import contextmanager

from src.host.device_registry import data_file

logger = logging.getLogger(__name__)

DB_PATH = data_file("openclaw.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    device_id   TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    params      TEXT DEFAULT '{}',
    result      TEXT,
    policy_id   TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_device  ON tasks(device_id);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

CREATE TABLE IF NOT EXISTS schedules (
    schedule_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    cron_expr   TEXT NOT NULL,
    task_type   TEXT NOT NULL,
    device_id   TEXT,
    params      TEXT DEFAULT '{}',
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_run    TEXT,
    next_run    TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_enabled ON schedules(enabled);
CREATE INDEX IF NOT EXISTS idx_schedules_next    ON schedules(next_run);

CREATE TABLE IF NOT EXISTS device_states (
    device_id   TEXT NOT NULL,
    platform    TEXT NOT NULL DEFAULT 'tiktok',
    key         TEXT NOT NULL,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, platform, key)
);
CREATE INDEX IF NOT EXISTS idx_dstate_device ON device_states(device_id, platform);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id  TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    category       TEXT NOT NULL DEFAULT 'general',
    status         TEXT NOT NULL DEFAULT 'active',
    variants       TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL,
    ended_at       TEXT
);

CREATE TABLE IF NOT EXISTS experiment_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id  TEXT NOT NULL,
    variant        TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    device_id      TEXT DEFAULT '',
    metadata       TEXT DEFAULT '{}',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exp_events_exp ON experiment_events(experiment_id, variant);
CREATE INDEX IF NOT EXISTS idx_exp_events_type ON experiment_events(experiment_id, event_type);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT DEFAULT '',
    detail      TEXT DEFAULT '',
    source      TEXT DEFAULT 'api',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);
CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit_logs(timestamp);

CREATE TABLE IF NOT EXISTS device_groups (
    group_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    color       TEXT DEFAULT '#60a5fa',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS device_group_members (
    group_id    TEXT NOT NULL,
    device_id   TEXT NOT NULL,
    added_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (group_id, device_id)
);
CREATE INDEX IF NOT EXISTS idx_dgm_group ON device_group_members(group_id);

CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'draft',
    target_accounts TEXT DEFAULT '[]',
    device_ids      TEXT DEFAULT '[]',
    task_sequence   TEXT DEFAULT '[]',
    params          TEXT DEFAULT '{}',
    message_template TEXT DEFAULT '',
    ai_rewrite      INTEGER DEFAULT 1,
    batch_id        TEXT DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    started_at      TEXT,
    completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);

CREATE TABLE IF NOT EXISTS seed_quality (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seed_username TEXT NOT NULL,
    device_id TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    follows_count INTEGER DEFAULT 0,
    replies_count INTEGER DEFAULT 0,
    referrals_count INTEGER DEFAULT 0,
    conversions_count INTEGER DEFAULT 0,
    last_used_at TEXT DEFAULT '',
    created_at TEXT DEFAULT '',
    UNIQUE(seed_username, device_id)
);
CREATE INDEX IF NOT EXISTS idx_seed_quality_country ON seed_quality(country, follows_count);

CREATE TABLE IF NOT EXISTS crm_interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'inbound',
    text TEXT DEFAULT '',
    intent TEXT DEFAULT '',
    device_id TEXT DEFAULT '',
    action TEXT DEFAULT '',
    platform TEXT DEFAULT 'tiktok',
    ts TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_crm_contact ON crm_interactions(contact);

CREATE TABLE IF NOT EXISTS ab_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment TEXT NOT NULL,
    variant TEXT NOT NULL,
    event_type TEXT NOT NULL,
    contact_id TEXT DEFAULT '',
    ts TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ab_exp ON ab_events(experiment, variant);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


_MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN batch_id TEXT DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN checkpoint TEXT DEFAULT ''",
    "CREATE TABLE IF NOT EXISTS campaigns (campaign_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'draft', target_accounts TEXT DEFAULT '[]', device_ids TEXT DEFAULT '[]', task_sequence TEXT DEFAULT '[]', params TEXT DEFAULT '{}', message_template TEXT DEFAULT '', ai_rewrite INTEGER DEFAULT 1, batch_id TEXT DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')), started_at TEXT, completed_at TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status)",
    # 任务优先级：0=最低, 50=默认, 100=高意向紧急回复
    "ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 50",
    "CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(status, priority DESC, created_at)",
    # 任务重试机制
    "ALTER TABLE tasks ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN max_retries INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN next_retry_at TEXT",
    "CREATE INDEX IF NOT EXISTS idx_tasks_retry ON tasks(status, next_retry_at) WHERE max_retries > 0",
    # 软删除（回收站）：非空表示已移入回收站
    "ALTER TABLE tasks ADD COLUMN deleted_at TEXT",
    "CREATE INDEX IF NOT EXISTS idx_tasks_deleted ON tasks(deleted_at) WHERE deleted_at IS NOT NULL",

    # ─── Sprint 2 P0: Facebook 业务表(漏斗 + AI 日报数据源) ───
    "CREATE TABLE IF NOT EXISTS facebook_groups ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " device_id TEXT NOT NULL,"
    " group_name TEXT NOT NULL,"
    " group_url TEXT DEFAULT '',"
    " member_count INTEGER DEFAULT 0,"
    " language TEXT DEFAULT '',"
    " country TEXT DEFAULT '',"
    " status TEXT NOT NULL DEFAULT 'joined',"  # joined/left/pending/banned
    " joined_at TEXT NOT NULL DEFAULT (datetime('now')),"
    " last_visited_at TEXT,"
    " visit_count INTEGER DEFAULT 0,"
    " extracted_member_count INTEGER DEFAULT 0,"
    " UNIQUE(device_id, group_name)"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_fb_groups_device ON facebook_groups(device_id)",
    "CREATE INDEX IF NOT EXISTS idx_fb_groups_status ON facebook_groups(status)",

    "CREATE TABLE IF NOT EXISTS facebook_friend_requests ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " device_id TEXT NOT NULL,"
    " target_name TEXT NOT NULL,"
    " target_profile_url TEXT DEFAULT '',"
    " note TEXT DEFAULT '',"
    " source TEXT DEFAULT '',"  # group_name / search_keyword / suggestion
    " status TEXT NOT NULL DEFAULT 'sent',"  # sent/accepted/rejected/cancelled/risk
    " sent_at TEXT NOT NULL DEFAULT (datetime('now')),"
    " accepted_at TEXT,"
    " lead_id INTEGER,"  # 对应 leads.db 中的 lead_id
    " UNIQUE(device_id, target_name, sent_at)"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_fb_fr_device ON facebook_friend_requests(device_id)",
    "CREATE INDEX IF NOT EXISTS idx_fb_fr_status ON facebook_friend_requests(status)",
    "CREATE INDEX IF NOT EXISTS idx_fb_fr_sent ON facebook_friend_requests(sent_at)",

    "CREATE TABLE IF NOT EXISTS facebook_inbox_messages ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " device_id TEXT NOT NULL,"
    " peer_name TEXT NOT NULL,"
    " peer_type TEXT DEFAULT 'friend',"  # friend/stranger(message_request)/group
    " message_text TEXT DEFAULT '',"
    " direction TEXT NOT NULL DEFAULT 'incoming',"  # incoming/outgoing
    " ai_decision TEXT DEFAULT '',"  # reply/skip/escalate/wa_referral
    " ai_reply_text TEXT DEFAULT '',"
    " language_detected TEXT DEFAULT '',"
    " seen_at TEXT NOT NULL DEFAULT (datetime('now')),"
    " replied_at TEXT,"
    " lead_id INTEGER"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_fb_inbox_device ON facebook_inbox_messages(device_id)",
    "CREATE INDEX IF NOT EXISTS idx_fb_inbox_peer ON facebook_inbox_messages(peer_name)",
    "CREATE INDEX IF NOT EXISTS idx_fb_inbox_seen ON facebook_inbox_messages(seen_at)",

    # ─── Sprint 3 P0: 给 friend_requests / inbox 加 preset_key,支持按预设切片 ───
    "ALTER TABLE facebook_friend_requests ADD COLUMN preset_key TEXT DEFAULT ''",
    "ALTER TABLE facebook_inbox_messages ADD COLUMN preset_key TEXT DEFAULT ''",
    "ALTER TABLE facebook_groups ADD COLUMN preset_key TEXT DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_fb_fr_preset ON facebook_friend_requests(preset_key, sent_at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_inbox_preset ON facebook_inbox_messages(preset_key, seen_at)",

    # ─── Sprint 4 P1: TikTok 漏斗事件统一表 ──────────────────────────────
    # 复制 FB 的埋点模式到 TK,让 /dashboard/cross-platform-funnel 两侧有
    # 真实可比较的 6 阶段数据,而不是从 tasks 表 COUNT 估算。
    # 设计原则:
    #   * 一张表统一 6 个 stage,方便 GROUP BY 聚合
    #   * stage 用字符串枚举(exposure/interest/engagement/direct_msg/
    #     guidance/conversion) 和 FB 的 get_funnel_metrics 的 stage_* 对齐
    #   * target_key 自由文本(username/video_id/dm_peer),用于去重
    #   * preset_key 复用 FB 的切片模式(warmup/growth/emergency_cooldown)
    """CREATE TABLE IF NOT EXISTS tiktok_funnel_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        target_key TEXT NOT NULL DEFAULT '',
        preset_key TEXT DEFAULT '',
        meta_json TEXT DEFAULT '',
        at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tt_funnel_stage ON tiktok_funnel_events(stage, at)",
    "CREATE INDEX IF NOT EXISTS idx_tt_funnel_device ON tiktok_funnel_events(device_id, at)",
    "CREATE INDEX IF NOT EXISTS idx_tt_funnel_preset ON tiktok_funnel_events(preset_key, stage, at)",

    # ─── 2026-04-21 P0-3: Facebook 风控事件落库（驱动自动冷却红旗）─────
    # 动机: `_report_risk` 原先只推 event_stream + 写 DeviceStateStore，
    # 无历史、无 24h 聚合，Gate 无法做"≥3 次/24h 自动冷却"。
    # kind: checkpoint / identity_verify / captcha / account_review /
    #       policy_warning / other（由关键词映射，见 fb_store._classify_risk_kind）
    """CREATE TABLE IF NOT EXISTS fb_risk_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        task_id TEXT DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'other',
        raw_message TEXT DEFAULT '',
        detected_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fb_risk_device_at ON fb_risk_events(device_id, detected_at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_risk_kind ON fb_risk_events(kind, detected_at)",

    # ─── 2026-04-21 P1-2: Facebook 账号阶段状态机 ──────────────────────
    # 每台设备（= FB 账号）一行。phase ∈ cold_start/growth/mature/cooldown。
    # 迁移规则见 config/facebook_playbook.yaml 的 phase_transitions，
    # 由 fb_account_phase.evaluate_transition() 事件驱动更新。
    """CREATE TABLE IF NOT EXISTS fb_account_phase (
        device_id TEXT PRIMARY KEY,
        phase TEXT NOT NULL DEFAULT 'cold_start',
        since_at TEXT NOT NULL DEFAULT (datetime('now')),
        first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
        total_scrolls INTEGER DEFAULT 0,
        total_likes INTEGER DEFAULT 0,
        total_risk_events INTEGER DEFAULT 0,
        last_task_at TEXT,
        last_risk_at TEXT,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fb_phase_phase ON fb_account_phase(phase)",

    # ─── 2026-04-21 P1-3a: Facebook campaign_run 断点续跑 ─────────────
    # 每次 facebook_campaign_run 任务一行。步骤完成情况写 state_json，
    # 失败/中断后，同一 run_id 再次提交 → 跳过已完成步骤。
    """CREATE TABLE IF NOT EXISTS fb_campaign_runs (
        run_id TEXT PRIMARY KEY,
        task_id TEXT DEFAULT '',
        device_id TEXT NOT NULL,
        preset_key TEXT DEFAULT '',
        total_steps INTEGER DEFAULT 0,
        current_step_idx INTEGER DEFAULT 0,
        current_step_name TEXT DEFAULT '',
        state TEXT NOT NULL DEFAULT 'running',
        state_json TEXT DEFAULT '{}',
        started_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        finished_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fb_campaign_device ON fb_campaign_runs(device_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_campaign_state ON fb_campaign_runs(state)",

    # ─── 2026-04-21 P2-4 Sprint A: 目标画像识别（日本 37-60 岁女性） ──────
    # 动机：把"谁值得互动"沉淀成可配置、可追溯、可审计的数据结构，
    # 不再用硬编码分支判断。Sprint A 先做 4 张表 + YAML 画像。
    #
    # 1) fb_target_personas: 目标画像定义（多画像，每行一个）
    #    配置来源为 config/fb_target_personas.yaml 热加载，
    #    本表仅作为"任务→使用了哪个画像 snapshot"的审计追溯。
    #    active=1 表示该画像当前启用，用于默认选中。
    """CREATE TABLE IF NOT EXISTS fb_target_personas (
        persona_key TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        age_min INTEGER DEFAULT 0,
        age_max INTEGER DEFAULT 120,
        gender TEXT DEFAULT 'any',
        locale TEXT DEFAULT '',
        rules_json TEXT DEFAULT '{}',
        vlm_prompt TEXT DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fb_persona_active ON fb_target_personas(active)",

    # 2) fb_profile_insights: 识别结果（每次对同一 target_key 的判断结果）
    #    stage ∈ L1/L2; match=1 表示判定命中画像；score ∈ [0,100]
    #    target_key 通常是 profile_url / user_id / username 其中一种
    #    insights_json 存 {age_band, gender, is_japanese, topics:[...], reasons:[...]}
    """CREATE TABLE IF NOT EXISTS fb_profile_insights (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        task_id TEXT DEFAULT '',
        persona_key TEXT NOT NULL,
        target_key TEXT NOT NULL,
        display_name TEXT DEFAULT '',
        stage TEXT NOT NULL DEFAULT 'L1',
        match INTEGER NOT NULL DEFAULT 0,
        score REAL NOT NULL DEFAULT 0,
        confidence REAL DEFAULT 0,
        insights_json TEXT DEFAULT '{}',
        image_paths TEXT DEFAULT '[]',
        vlm_model TEXT DEFAULT '',
        latency_ms INTEGER DEFAULT 0,
        classified_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fb_insights_persona ON fb_profile_insights(persona_key, classified_at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_insights_target ON fb_profile_insights(target_key, classified_at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_insights_device ON fb_profile_insights(device_id, stage, classified_at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_insights_match ON fb_profile_insights(match, classified_at)",

    # 3) fb_content_exposure: 浏览内容主题曝光日志
    #    browse_feed 里看到的 post 封面做主题识别后落库，
    #    用于后续"按账号兴趣曲线决定点赞/互动倾向"。
    """CREATE TABLE IF NOT EXISTS fb_content_exposure (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        task_id TEXT DEFAULT '',
        topic TEXT NOT NULL DEFAULT 'other',
        lang TEXT DEFAULT '',
        liked INTEGER DEFAULT 0,
        dwell_ms INTEGER DEFAULT 0,
        meta_json TEXT DEFAULT '{}',
        seen_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fb_expose_device ON fb_content_exposure(device_id, seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_expose_topic ON fb_content_exposure(topic, seen_at)",

    # 4) ai_cost_events: AI 调用审计（本地 Ollama 记 0 USD，但记条数/耗时/显存）
    #    保留扩展 provider=openai/gemini/anthropic 做云调用时的成本追踪。
    """CREATE TABLE IF NOT EXISTS ai_cost_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL DEFAULT 'ollama',
        model TEXT NOT NULL DEFAULT '',
        task_id TEXT DEFAULT '',
        scene TEXT DEFAULT '',
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        image_count INTEGER DEFAULT 0,
        latency_ms INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0,
        ok INTEGER NOT NULL DEFAULT 1,
        error TEXT DEFAULT '',
        at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ai_cost_provider ON ai_cost_events(provider, at)",
    "CREATE INDEX IF NOT EXISTS idx_ai_cost_scene ON ai_cost_events(scene, at)",

    # ─── P2-4 Sprint C-1: VLM 并发排队指标 + 设备维度 ───
    "ALTER TABLE ai_cost_events ADD COLUMN queue_wait_ms INTEGER DEFAULT 0",
    "ALTER TABLE ai_cost_events ADD COLUMN device_id TEXT DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_ai_cost_device ON ai_cost_events(device_id, at)",

    # ─── 2026-04-23: 打招呼(greeting)特化列 ─────────────────────────────
    # 动机:
    #   * seen_at 对 outgoing 语义不清(真实意义是"发出时间")。新增专用 sent_at
    #     列,count_outgoing_messages_since 用它替代 seen_at,漏斗也更精确。
    #   * template_id 记录本次打招呼从哪条 chat_messages.yaml 模板抽的,
    #     供 A/B 效果分析。格式 "<country>:<index>"(如 "jp:3"),不强约束。
    # 向后兼容: 老行 sent_at=NULL, template_id='' 都不影响现有查询。
    "ALTER TABLE facebook_inbox_messages ADD COLUMN sent_at TEXT",
    "ALTER TABLE facebook_inbox_messages ADD COLUMN template_id TEXT DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_fb_inbox_sent_at ON facebook_inbox_messages(device_id, direction, sent_at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_inbox_template ON facebook_inbox_messages(template_id)",
]


def init_db():
    """建表（幂等）+ 增量迁移。服务启动时调用一次。"""
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass
        conn.commit()
        logger.info("数据库初始化完成: %s", DB_PATH)
    finally:
        conn.close()


@contextmanager
def get_conn():
    """上下文管理器，自动 commit/rollback/close。"""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
