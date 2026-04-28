# -*- coding: utf-8 -*-
"""4 数字日报 (Phase 22, 2026-04-27).

每天给老板/M 发一封邮件, 含 4 个核心数字:
  1. 加好友请求发送数 (add_friend_sent)
  2. 加好友被接受数 (add_friend_accepted) + 接受率
  3. Messenger 首次回复数 (greeting_replied) + 回复率
  4. LINE 引流发送数 (wa_referral_sent / line_dispatch_planned)

数据源 fb_contact_events 表; 按 device_id / persona_key 切片.

调用:
  from src.host.daily_report import get_daily_4_numbers, format_daily_text
  stats = get_daily_4_numbers(date_str="2026-04-27")
  print(format_daily_text(stats))

Cron 配置:
  0 9 * * *  python -m src.host.daily_report --send-email
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Phase 22: 4 数字日报关心的 event_type
EVT_FRIEND_SENT = "add_friend_sent"
EVT_FRIEND_ACCEPTED = "add_friend_accepted"
EVT_GREETING_SENT = "greeting_sent"
EVT_GREETING_REPLIED = "greeting_replied"
EVT_REFERRAL_SENT = "wa_referral_sent"
EVT_REFERRAL_REPLIED = "wa_referral_replied"
EVT_LINE_DISPATCH = "line_dispatch_planned"


def _date_window_iso(date_str: Optional[str] = None) -> tuple:
    """返回 (start_iso, end_iso) 标识当日 UTC 时间窗.

    None → 今天; 否则按 'YYYY-MM-DD' 解析.
    """
    if date_str:
        try:
            d = _dt.datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError as e:
            raise ValueError(f"date_str 必须是 YYYY-MM-DD: {date_str}") from e
    else:
        d = _dt.datetime.utcnow().date()
    start = _dt.datetime.combine(d, _dt.time.min)
    end = start + _dt.timedelta(days=1)
    return (start.strftime("%Y-%m-%d %H:%M:%S"),
            end.strftime("%Y-%m-%d %H:%M:%S"))


def _count_events(conn, evt_types: List[str], start_iso: str, end_iso: str,
                    *, device_id: Optional[str] = None) -> int:
    """fb_contact_events 在时间窗内某 event_type 的独立 peer 数."""
    where = ["event_type IN ({})".format(",".join("?" * len(evt_types))),
             "at >= ?", "at < ?"]
    args: List[Any] = list(evt_types) + [start_iso, end_iso]
    if device_id:
        where.append("device_id = ?")
        args.append(device_id)
    sql = ("SELECT COUNT(DISTINCT peer_name) AS n"
           " FROM fb_contact_events WHERE " + " AND ".join(where))
    row = conn.execute(sql, args).fetchone()
    return int(row["n"] if row else 0)


def get_daily_4_numbers(date_str: Optional[str] = None,
                         *, device_id: Optional[str] = None) -> Dict[str, Any]:
    """跑 4 数字 SQL, 返 dict.

    Returns:
      {
        "date": "2026-04-27",
        "device_id": "" / "abcdef",
        "friend_requests_sent": int,
        "friend_accepted": int,
        "accept_rate": float,
        "greeting_replied": int,
        "reply_rate": float,
        "line_invites_sent": int,
        "invite_rate": float,
        "raw_events_in_window": int,
      }
    """
    from src.host.database import _connect

    start_iso, end_iso = _date_window_iso(date_str)
    actual_date = start_iso.split(" ", 1)[0]

    with _connect() as conn:
        sent = _count_events(conn, [EVT_FRIEND_SENT],
                              start_iso, end_iso, device_id=device_id)
        accepted = _count_events(conn, [EVT_FRIEND_ACCEPTED],
                                  start_iso, end_iso, device_id=device_id)
        greet_sent = _count_events(conn, [EVT_GREETING_SENT],
                                    start_iso, end_iso, device_id=device_id)
        greet_replied = _count_events(conn, [EVT_GREETING_REPLIED],
                                       start_iso, end_iso, device_id=device_id)
        # LINE 邀请: 优先 line_dispatch_planned, fallback wa_referral_sent
        line_dispatch = _count_events(conn, [EVT_LINE_DISPATCH],
                                        start_iso, end_iso, device_id=device_id)
        wa_sent = _count_events(conn, [EVT_REFERRAL_SENT],
                                  start_iso, end_iso, device_id=device_id)
        line_invites = max(line_dispatch, wa_sent)

        # raw count for sanity check
        where_args = [start_iso, end_iso]
        where_sql = "at >= ? AND at < ?"
        if device_id:
            where_sql += " AND device_id = ?"
            where_args.append(device_id)
        raw = conn.execute(
            f"SELECT COUNT(*) AS n FROM fb_contact_events WHERE {where_sql}",
            where_args,
        ).fetchone()
        raw_n = int(raw["n"] if raw else 0)

    accept_rate = (accepted / sent) if sent else 0.0
    # reply rate: 首回 / 已发的 greeting (而非已接受的好友, 因为 accepted ≠ greeting_sent)
    reply_rate = (greet_replied / greet_sent) if greet_sent else 0.0
    invite_rate = (line_invites / greet_replied) if greet_replied else 0.0

    return {
        "date": actual_date,
        "device_id": device_id or "",
        "friend_requests_sent": sent,
        "friend_accepted": accepted,
        "accept_rate": round(accept_rate, 4),
        "greeting_sent": greet_sent,
        "greeting_replied": greet_replied,
        "reply_rate": round(reply_rate, 4),
        "line_invites_sent": line_invites,
        "invite_rate": round(invite_rate, 4),
        "raw_events_in_window": raw_n,
    }


def format_daily_text(stats: Dict[str, Any]) -> str:
    """格式化 4 数字成纯文本(邮件 body 用).

    Returns: 多行字符串
    """
    lines = [
        f"📊 OpenClaw 日报 - {stats['date']}",
        f"   设备: {stats['device_id'] or '全部 21 台'}",
        "",
        "─────────────────────────────────",
        f"📤 加好友请求发送   {stats['friend_requests_sent']:>6}",
        f"✅ 被对方接受       {stats['friend_accepted']:>6}  "
        f"({stats['accept_rate']*100:.1f}%)",
        f"💬 收到首次回复     {stats['greeting_replied']:>6}  "
        f"({stats['reply_rate']*100:.1f}%)",
        f"🎯 LINE 邀请发送    {stats['line_invites_sent']:>6}  "
        f"({stats['invite_rate']*100:.1f}%)",
        "─────────────────────────────────",
        f"   原始事件数      {stats['raw_events_in_window']:>6}",
        "",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━",
        "Generated by OpenClaw Phase 22 daily_report",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI: python -m src.host.daily_report [--date 2026-04-27] [--device-id X]
                                          [--send-email recipient@example.com]
    """
    parser = argparse.ArgumentParser(description="OpenClaw 4 数字日报")
    parser.add_argument("--date", default=None,
                        help="YYYY-MM-DD, 默认今日")
    parser.add_argument("--device-id", default=None,
                        help="按设备过滤; 不传 = 全部")
    parser.add_argument("--send-email", default=None,
                        help="收件人邮箱; 不传 = 仅控制台输出")
    parser.add_argument("--json", action="store_true",
                        help="输出 JSON 而非文本")
    args = parser.parse_args(argv)

    try:
        stats = get_daily_4_numbers(date_str=args.date,
                                       device_id=args.device_id)
    except Exception as e:
        print(f"[daily_report] 失败: {e}", file=sys.stderr)
        return 1

    if args.json:
        import json
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        print(format_daily_text(stats))

    if args.send_email:
        try:
            # MVP: 邮件发送借用现有 monitoring/alerts 通道
            # 暂留 stub, 由运营手动接 SMTP/SendGrid
            logger.info("[daily_report] email stub: would send to %s",
                         args.send_email)
            print(f"\n(--send-email={args.send_email} requested but SMTP "
                  f"integration is a stub; wire up in next phase)",
                  file=sys.stderr)
        except Exception as e:
            print(f"[daily_report] email 失败: {e}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
