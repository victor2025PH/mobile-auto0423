// -*- coding: utf-8 -*-
/**
 * Lead Mesh Dashboard UI (Phase 5.5 · 2026-04-23)
 *
 * 三大视图:
 *   1. 接收方工作台 (lmOpenHandoffInbox)   — 按接收方账号聚合的待处理队列
 *   2. Lead 档案 / 时间轴 (lmOpenLeadSearch / lmOpenLeadDossier)
 *   3. 运营指挥台 (lmOpenCommandCenter)     — 漏斗 + 接收方负载 + 告警
 *
 * 全部 window.lm* 挂载, 纯 JS + innerHTML, 沿用 PlatShell 公共组件。
 */
(function () {
  'use strict';

  // ─── Shell 引用 + helpers ──────────────────────────────────────
  function _shell() {
    const s = window.PlatShell;
    if (!s) { showToast && showToast('PlatShell 未加载', 'error'); return null; }
    return s;
  }

  function _fmtTime(iso) {
    if (!iso) return '-';
    try {
      const d = new Date(iso.replace(' ', 'T').replace(/Z?$/, 'Z'));
      const now = new Date();
      const diffMin = Math.round((now - d) / 60000);
      if (diffMin < 60) return diffMin + ' 分钟前';
      if (diffMin < 1440) return Math.round(diffMin / 60) + ' 小时前';
      return Math.round(diffMin / 1440) + ' 天前';
    } catch (e) { return iso; }
  }

  function _safe(s) {
    return String(s == null ? '' : s).replace(/[<>&"']/g, function (c) {
      return { '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  const _ACTION_ICON = {
    extracted: '🟢', friend_requested: '🤝', friend_accepted: '✅',
    friend_rejected: '❌',
    greeting_sent: '✉️', greeting_fallback: '🔄', greeting_replied: '💬',
    inbox_received: '📨', reply_sent: '📤',
    referral_sent: '🔗', referral_blocked: '🚫',
    handoff_created: '📤', handoff_acknowledged: '👀',
    handoff_completed: '✅', handoff_rejected: '❌', handoff_expired: '⏰',
    lead_merged: '🔀', lead_marked_duplicate: '🔗',
    human_intervention: '👤', risk_detected: '⚠️',
  };
  const _ACTION_COLOR = {
    handoff_created: '#0ea5e9', handoff_completed: '#22c55e',
    handoff_rejected: '#ef4444', handoff_expired: '#f59e0b',
    risk_detected: '#ef4444', referral_blocked: '#ef4444',
    greeting_replied: '#22d3ee', referral_sent: '#a855f7',
  };

  const _STATE_COLOR = {
    pending: '#f59e0b', acknowledged: '#0ea5e9',
    completed: '#22c55e', rejected: '#ef4444',
    expired: '#64748b', duplicate_blocked: '#94a3b8',
  };

  const _STATE_LABEL_ZH = {
    pending: '待处理', acknowledged: '已确认', completed: '已完成',
    rejected: '已拒接', expired: '已过期', duplicate_blocked: '重复拦截',
  };


  // ─────────────────────────────────────────────────────────────────
  // P0 · 接收方工作台 (Handoff Inbox)
  // ─────────────────────────────────────────────────────────────────

  let _lmInboxState = { receiver: '', tab: 'pending', autoTimer: null };

  function _lmStopInboxAutoRefresh() {
    if (_lmInboxState.autoTimer) {
      clearInterval(_lmInboxState.autoTimer);
      _lmInboxState.autoTimer = null;
    }
  }

  window.lmOpenHandoffInbox = async function (receiverKey) {
    const Shell = _shell();
    if (!Shell) return;
    if (receiverKey) _lmInboxState.receiver = receiverKey;
    Shell.modal.open('lm-inbox-modal',
      '<div id="lm-inbox-body" style="padding:18px;">加载中…</div>',
      { maxWidth: '1100px' });
    await _lmRenderInbox();

    // 自动刷新: 每 30s 静默刷新一次, 让运营能看到新进来的 handoff。
    // 闭模态时 (DOM 不在了) 自动清理。
    _lmStopInboxAutoRefresh();
    _lmInboxState.autoTimer = setInterval(function () {
      const m = document.getElementById('lm-inbox-modal');
      if (!m) { _lmStopInboxAutoRefresh(); return; }
      // 静默刷新 — 如果用户正在 hover 某张卡片, 保留其 details 展开状态
      const openedDetails = new Set();
      document.querySelectorAll('#lm-inbox-body details[open]').forEach(function (el) {
        const card = el.closest('[id^="lm-card-"]');
        if (card) openedDetails.add(card.id);
      });
      _lmRenderInbox().then(function () {
        openedDetails.forEach(function (cid) {
          const card = document.getElementById(cid);
          if (card) {
            const d = card.querySelector('details');
            if (d) d.open = true;
          }
        });
      });
    }, 30000);
  };

  async function _lmRenderInbox() {
    const Shell = _shell();
    const body = document.getElementById('lm-inbox-body');
    if (!body) return;
    body.innerHTML = '加载中…';
    try {
      // 拉取所有状态以展示 Tab 计数
      const stateQs = '';  // 全量拉来分 tab
      const receiver = _lmInboxState.receiver;
      const recvQs = receiver ? ('&receiver_account_key=' + encodeURIComponent(receiver)) : '';
      const [p, a, c, r] = await Promise.all([
        Shell.api.get('/lead-mesh/handoffs?state=pending&limit=200' + recvQs),
        Shell.api.get('/lead-mesh/handoffs?state=acknowledged&limit=200' + recvQs),
        Shell.api.get('/lead-mesh/handoffs?state=completed&limit=100' + recvQs),
        Shell.api.get('/lead-mesh/handoffs?state=rejected&limit=100' + recvQs),
      ]);
      const pending = (p && p.handoffs) || [];
      const ack = (a && a.handoffs) || [];
      const completed = (c && c.handoffs) || [];
      const rejected = (r && r.handoffs) || [];

      const tab = _lmInboxState.tab;
      const list = tab === 'pending' ? pending
                 : tab === 'acknowledged' ? ack
                 : tab === 'completed' ? completed
                 : rejected;

      // 收集所有 receiver 的 key (去重) 供下拉
      const receiverSet = new Set();
      [pending, ack, completed, rejected].forEach(function (arr) {
        arr.forEach(function (h) {
          if (h.receiver_account_key) receiverSet.add(h.receiver_account_key);
        });
      });
      const receivers = Array.from(receiverSet).sort();

      const tabBtn = function (key, label, count, color) {
        const active = tab === key;
        return '<button onclick="lmSwitchInboxTab(\'' + key + '\')"'
          + ' style="padding:8px 16px;background:' + (active ? color : 'transparent')
          + ';color:' + (active ? '#fff' : 'var(--text)')
          + ';border:1px solid ' + color + ';border-radius:8px;font-size:13px;'
          + 'font-weight:' + (active ? '600' : '400') + ';cursor:pointer;margin-right:6px">'
          + label + ' <span style="font-size:11px;opacity:0.85">(' + count + ')</span></button>';
      };

      const receiverSelect = '<select id="lm-inbox-receiver" onchange="lmSwitchInboxReceiver(this.value)" '
        + 'style="background:var(--bg-card);border:1px solid var(--border);color:var(--text);'
        + 'padding:5px 10px;border-radius:6px;font-size:12px;min-width:180px">'
        + '<option value="">— 所有接收方 —</option>'
        + receivers.map(function (r) {
            return '<option value="' + _safe(r) + '"' + (r === receiver ? ' selected' : '') + '>' + _safe(r) + '</option>';
          }).join('')
        + '</select>';

      const cards = list.length === 0
        ? '<div style="text-align:center;padding:40px;color:var(--text-dim)">暂无 ' + _STATE_LABEL_ZH[tab] + ' 交接单</div>'
        : list.map(_lmHandoffCardHtml).join('');

      body.innerHTML = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '  <div>'
        + '    <div style="font-size:18px;font-weight:700">🤝 接收方工作台</div>'
        + '    <div style="font-size:11px;color:var(--text-muted);margin-top:2px">'
        + '      下发到接收账号 → 标记 已看到 → 已接上 → 完成引流</div>'
        + '  </div>'
        + '  <button onclick="PlatShell.modal.close(\'lm-inbox-modal\')" '
        + '          style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '</div>'
        + '<div style="display:flex;gap:10px;align-items:center;margin-bottom:14px;padding:10px 12px;'
        + '            background:var(--bg-main);border-radius:8px;flex-wrap:wrap">'
        + '  <span style="font-size:12px;color:var(--text-muted)">📬 接收账号:</span>'
        + receiverSelect
        + '  <span style="margin-left:auto;font-size:11px;color:var(--text-dim)">'
        + '    pending=' + pending.length + ' ack=' + ack.length + ' done=' + completed.length + '</span>'
        + '  <button onclick="lmRefreshInbox()" '
        + '          style="padding:5px 10px;background:rgba(96,165,250,.15);color:#60a5fa;border:1px solid rgba(96,165,250,.4);border-radius:6px;font-size:11px;cursor:pointer">🔄 刷新</button>'
        + '</div>'
        + '<div style="display:flex;margin-bottom:14px;flex-wrap:wrap;gap:4px">'
        + tabBtn('pending', _STATE_LABEL_ZH.pending, pending.length, _STATE_COLOR.pending)
        + tabBtn('acknowledged', _STATE_LABEL_ZH.acknowledged, ack.length, _STATE_COLOR.acknowledged)
        + tabBtn('completed', _STATE_LABEL_ZH.completed, completed.length, _STATE_COLOR.completed)
        + tabBtn('rejected', _STATE_LABEL_ZH.rejected, rejected.length, _STATE_COLOR.rejected)
        + '</div>'
        + '<div style="display:grid;gap:10px;max-height:60vh;overflow-y:auto">'
        + cards
        + '</div>';
    } catch (e) {
      body.innerHTML = '<div style="color:#ef4444;padding:20px">加载失败: ' + _safe(e.message || e) + '</div>';
    }
  }

  function _lmHandoffCardHtml(h) {
    const state = h.state || 'pending';
    const color = _STATE_COLOR[state] || '#60a5fa';
    const snap = h.conversation_snapshot || [];
    const snapCount = snap.length;
    const hid = h.handoff_id || '';
    const actions = (state === 'pending' || state === 'acknowledged')
      ? '<div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">'
        + (state === 'pending'
           ? '<button onclick="lmHandoffAction(\'' + hid + '\', \'acknowledge\')" '
             + 'style="padding:6px 14px;background:rgba(14,165,233,.15);color:#0ea5e9;border:1px solid rgba(14,165,233,.4);border-radius:6px;font-size:12px;cursor:pointer">👀 已看到</button>'
           : '')
        + '<button onclick="lmHandoffAction(\'' + hid + '\', \'complete\')" '
        + 'style="padding:6px 14px;background:rgba(34,197,94,.15);color:#22c55e;border:1px solid rgba(34,197,94,.4);border-radius:6px;font-size:12px;cursor:pointer;font-weight:600">✅ 已接上</button>'
        + '<button onclick="lmHandoffAction(\'' + hid + '\', \'reject\')" '
        + 'style="padding:6px 14px;background:rgba(239,68,68,.1);color:#ef4444;border:1px solid rgba(239,68,68,.3);border-radius:6px;font-size:12px;cursor:pointer">❌ 拒接</button>'
        + '</div>'
      : '';

    return ''
      + '<div id="lm-card-' + hid + '" style="background:var(--bg-main);border:1px solid var(--border);'
      + '  border-left:4px solid ' + color + ';border-radius:10px;padding:14px">'
      + '  <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px">'
      + '    <div style="flex:1;min-width:0">'
      + '      <div style="font-weight:600;font-size:14px;margin-bottom:4px">'
      + '        📌 ' + _safe(hid.substring(0, 12)) + '…'
      + '        <span style="margin-left:8px;font-size:11px;padding:2px 8px;background:rgba(0,0,0,.2);border-radius:4px;color:' + color + '">'
      +            _STATE_LABEL_ZH[state] + '</span>'
      + '      </div>'
      + '      <div style="font-size:12px;color:var(--text-muted);margin-bottom:4px">'
      + '        来自 <code>' + _safe(h.source_agent) + '</code>'
      +          (h.source_device ? '@<code>' + _safe(h.source_device.substring(0, 8)) + '</code>' : '')
      + '        · 渠道 <b style="color:#0ea5e9">' + _safe(h.channel) + '</b>'
      + '        · 接收方 <code>' + _safe(h.receiver_account_key || '未指派') + '</code>'
      + '      </div>'
      + '      <div style="font-size:11px;color:var(--text-dim)">'
      + '        🕒 ' + _fmtTime(h.created_at) + ' · 聊天 ' + snapCount + ' 轮'
      + '      </div>'
      + '      <details style="margin-top:8px;font-size:12px">'
      + '        <summary style="cursor:pointer;color:#60a5fa">💬 展开聊天 + 引流内容</summary>'
      + '        <div style="margin-top:8px;padding:10px;background:var(--bg-card);border-radius:6px">'
      + '          <div style="font-size:11px;color:var(--text-dim);margin-bottom:6px">引流话术:</div>'
      + '          <div style="padding:6px 10px;background:rgba(168,85,247,.1);border-radius:4px;font-size:12px;margin-bottom:8px;white-space:pre-wrap">'
      +              _safe(h.snippet_sent || '(无)') + '</div>'
      + '          <div style="font-size:11px;color:var(--text-dim);margin-bottom:6px">最近对话 (已脱敏):</div>'
      +            snap.map(function (t) {
                     const dir = t.direction === 'outgoing' ? '→' : '←';
                     const dcol = t.direction === 'outgoing' ? '#22d3ee' : '#f59e0b';
                     const txt = t.text || t.message_text || '';
                     return '<div style="padding:4px 0;font-size:11px">'
                       + '<span style="color:' + dcol + ';font-weight:600">' + dir + '</span> '
                       + _safe(txt) + '</div>';
                   }).join('')
      + '        </div>'
      + '      </details>'
      + '    </div>'
      + '    <button onclick="lmOpenLeadDossier(\'' + _safe(h.canonical_id) + '\')" '
      + '            style="padding:5px 10px;background:none;border:1px solid var(--border);color:var(--text-muted);border-radius:6px;font-size:11px;cursor:pointer;white-space:nowrap">'
      + '      🔍 Lead 档案</button>'
      + '  </div>'
      + actions
      + '</div>';
  }

  window.lmSwitchInboxTab = function (tab) {
    _lmInboxState.tab = tab;
    _lmRenderInbox();
  };
  window.lmSwitchInboxReceiver = function (r) {
    _lmInboxState.receiver = r;
    _lmRenderInbox();
  };
  window.lmRefreshInbox = function () { _lmRenderInbox(); };

  window.lmHandoffAction = async function (handoffId, action) {
    const Shell = _shell();
    if (!Shell) return;
    const actionLabel = {acknowledge: '标记已看到', complete: '标记已接上', reject: '拒接'}[action] || action;
    if (!confirm('确认 ' + actionLabel + ' handoff ' + handoffId.substring(0, 12) + '… ?')) return;
    try {
      await Shell.api.post('/lead-mesh/handoffs/' + handoffId + '/' + action,
                            { by: 'human:dashboard' });
      showToast(actionLabel + ' 成功', 'success');
      _lmRenderInbox();
    } catch (e) {
      showToast(actionLabel + ' 失败: ' + (e.message || e), 'error');
    }
  };


  // ─────────────────────────────────────────────────────────────────
  // P1 · Lead 档案搜索 + 时间轴
  // ─────────────────────────────────────────────────────────────────

  window.lmOpenLeadSearch = async function () {
    const Shell = _shell();
    if (!Shell) return;
    Shell.modal.open('lm-search-modal', ''
      + '<div style="padding:18px">'
      + '  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
      + '    <div style="font-size:18px;font-weight:700">🔍 Lead 档案搜索</div>'
      + '    <button onclick="PlatShell.modal.close(\'lm-search-modal\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
      + '  </div>'
      + '  <div style="display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap">'
      + '    <input id="lm-search-name" placeholder="名字(模糊)…" '
      + '           onkeydown="if(event.key===\'Enter\')lmDoSearch()" '
      + '           style="flex:1;min-width:180px;padding:8px 12px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px">'
      + '    <select id="lm-search-platform" style="padding:8px 12px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px">'
      + '      <option value="">所有平台</option>'
      + '      <option value="facebook">Facebook</option>'
      + '      <option value="line">LINE</option>'
      + '      <option value="whatsapp">WhatsApp</option>'
      + '      <option value="telegram">Telegram</option>'
      + '      <option value="instagram">Instagram</option>'
      + '    </select>'
      + '    <input id="lm-search-account" placeholder="账号 id (模糊)…" '
      + '           onkeydown="if(event.key===\'Enter\')lmDoSearch()" '
      + '           style="min-width:160px;padding:8px 12px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px">'
      + '    <button onclick="lmDoSearch()" '
      + '            style="padding:8px 20px;background:#0ea5e9;color:#fff;border:none;border-radius:6px;font-weight:600;cursor:pointer">搜索</button>'
      + '  </div>'
      + '  <div id="lm-search-results" style="max-height:60vh;overflow-y:auto"></div>'
      + '</div>',
      { maxWidth: '900px' });
    setTimeout(function () {
      const el = document.getElementById('lm-search-name');
      if (el) el.focus();
    }, 150);
  };

  window.lmDoSearch = async function () {
    const Shell = _shell();
    if (!Shell) return;
    const name = (document.getElementById('lm-search-name') || {}).value || '';
    const platform = (document.getElementById('lm-search-platform') || {}).value || '';
    const account = (document.getElementById('lm-search-account') || {}).value || '';
    const box = document.getElementById('lm-search-results');
    if (!box) return;
    box.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-dim)">搜索中…</div>';
    try {
      const qs = [];
      if (name) qs.push('name_like=' + encodeURIComponent(name));
      if (platform) qs.push('platform=' + encodeURIComponent(platform));
      if (account) qs.push('account_id_like=' + encodeURIComponent(account));
      const r = await Shell.api.get('/lead-mesh/leads/search?' + qs.join('&'));
      const results = (r && r.results) || [];
      if (results.length === 0) {
        box.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-dim)">无匹配</div>';
        return;
      }
      box.innerHTML = '<div style="display:grid;gap:8px">'
        + results.map(function (r) {
            return ''
              + '<div onclick="lmOpenLeadDossier(\'' + _safe(r.canonical_id) + '\')" '
              + '     style="padding:12px 16px;background:var(--bg-main);border:1px solid var(--border);border-radius:8px;cursor:pointer;transition:border-color .15s"'
              + '     onmouseover="this.style.borderColor=\'#0ea5e9\'" '
              + '     onmouseout="this.style.borderColor=\'var(--border)\'">'
              + '  <div style="display:flex;justify-content:space-between">'
              + '    <div>'
              + '      <div style="font-weight:600">' + _safe(r.primary_name || '(无名)') + '</div>'
              + '      <div style="font-size:11px;color:var(--text-muted);margin-top:2px">'
              +         'cid <code>' + _safe(r.canonical_id.substring(0, 12)) + '…</code>'
              +         ' · lang:' + _safe(r.primary_language || '?')
              +         ' · persona:' + _safe(r.primary_persona_key || '?') + '</div>'
              + '    </div>'
              + '    <div style="font-size:11px;color:var(--text-dim)">' + _fmtTime(r.created_at) + '</div>'
              + '  </div>'
              + '</div>';
          }).join('') + '</div>';
    } catch (e) {
      box.innerHTML = '<div style="color:#ef4444;padding:20px">搜索失败: ' + _safe(e.message || e) + '</div>';
    }
  };

  window.lmOpenLeadDossier = async function (canonicalId) {
    const Shell = _shell();
    if (!Shell) return;
    Shell.modal.open('lm-dossier-modal',
      '<div id="lm-dossier-body" style="padding:18px">加载中…</div>',
      { maxWidth: '1000px' });
    const body = document.getElementById('lm-dossier-body');
    try {
      const d = await Shell.api.get('/lead-mesh/leads/' + encodeURIComponent(canonicalId) + '?journey_limit=200');
      const canonical = d.canonical || {};
      const identities = d.identities || [];
      const journey = d.journey || [];
      const handoffs = d.handoffs || [];
      const summary = d.journey_summary || {};

      // 身份列表
      const idPlatformIcon = { facebook: '📘', line: '💬', whatsapp: '📱',
                                telegram: '✈️', instagram: '📷', messenger: '💬' };
      const idsHtml = identities.map(function (i) {
        return '<span style="display:inline-block;padding:4px 10px;background:rgba(96,165,250,.12);border-radius:4px;font-size:11px;margin:2px">'
          + (idPlatformIcon[i.platform] || '🔗') + ' ' + _safe(i.platform)
          + ': <code>' + _safe(i.account_id) + '</code>'
          + (i.verified ? '' : ' <span style="color:#f59e0b">(soft)</span>')
          + '</span>';
      }).join('');

      // 时间轴 (按天分组 - Phase 6 UX)
      const timeline = _lmTimelineHtml(journey);

      // 统计
      const statsHtml = Object.entries(summary.by_action || {}).sort(function (a, b) { return b[1] - a[1]; })
        .slice(0, 8)
        .map(function (kv) {
          return '<span style="display:inline-block;padding:2px 8px;background:var(--bg-card);border-radius:4px;font-size:11px;margin:2px">'
            + (_ACTION_ICON[kv[0]] || '') + ' ' + _safe(kv[0]) + ' ×' + kv[1] + '</span>';
        }).join('');

      body.innerHTML = ''
        + '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px">'
        + '  <div>'
        + '    <div style="font-size:18px;font-weight:700">📋 ' + _safe(canonical.primary_name || '(无名)') + '</div>'
        + '    <div style="font-size:11px;color:var(--text-muted);margin-top:2px">'
        + '      <code>' + _safe(canonical.canonical_id) + '</code>'
        +        (canonical.merged_into ? ' <span style="color:#f59e0b">已合并 → ' + _safe(canonical.merged_into.substring(0, 12)) + '</span>' : '')
        + '    </div>'
        + '  </div>'
        + '  <button onclick="PlatShell.modal.close(\'lm-dossier-modal\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '</div>'
        + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px">'
        + '  <div>'
        + '    <div style="font-size:12px;color:var(--text-muted);margin-bottom:6px">🔗 跨平台身份 ('
        +        identities.length + ')</div>'
        + '    <div>' + (idsHtml || '<span style="color:var(--text-dim)">无</span>') + '</div>'
        + '  </div>'
        + '  <div>'
        + '    <div style="font-size:12px;color:var(--text-muted);margin-bottom:6px">📊 事件分布 (top 8)</div>'
        + '    <div>' + statsHtml + '</div>'
        + '    <div style="font-size:11px;color:var(--text-dim);margin-top:4px">当前 owner: <code>'
        +         _safe(d.current_owner || '-') + '</code></div>'
        + '  </div>'
        + '</div>'
        + '<div style="font-size:12px;color:var(--text-muted);margin-bottom:6px">⏱ 时间轴 ('
        +    journey.length + ' 事件)</div>'
        + '<div style="max-height:50vh;overflow-y:auto;background:var(--bg-main);padding:10px;border-radius:8px">'
        + (timeline || '<div style="text-align:center;padding:20px;color:var(--text-dim)">无事件</div>')
        + '</div>'
        + (handoffs.length ? ''
          + '<div style="margin-top:14px;font-size:12px;color:var(--text-muted);margin-bottom:6px">🤝 交接记录 ('
          + handoffs.length + ')</div>'
          + '<div style="display:grid;gap:6px">'
          + handoffs.map(function (h) {
              return '<div style="padding:8px 12px;background:var(--bg-main);border-left:3px solid '
                + (_STATE_COLOR[h.state] || '#64748b')
                + ';border-radius:6px;font-size:11px">'
                + '<b>' + _safe(h.channel) + '</b> → '
                + _safe(h.receiver_account_key || '未指派')
                + ' · <span style="color:' + (_STATE_COLOR[h.state] || '#64748b') + '">' + _STATE_LABEL_ZH[h.state] + '</span>'
                + ' · ' + _fmtTime(h.created_at)
                + ' · <code style="color:var(--text-dim)">' + _safe(h.handoff_id.substring(0, 12)) + '</code>'
                + '</div>';
            }).join('')
          + '</div>' : '');
    } catch (e) {
      body.innerHTML = '<div style="color:#ef4444;padding:20px">加载失败: ' + _safe(e.message || e) + '</div>';
    }
  };


  // ─────────────────────────────────────────────────────────────────
  // P2 · 运营指挥台
  // ─────────────────────────────────────────────────────────────────

  // Phase 8d/8g: Command Center 过滤器状态 (保留在 window 作用域, 切换时重渲染)
  //   date (Phase 8g): 点 sparkline 某天后, 所有 funnel API 加 date=X 下钻
  window._lmCCFilter = window._lmCCFilter || { days: 7, actor: 'agent_a', date: '' };

  window.lmOpenCommandCenter = async function () {
    const Shell = _shell();
    if (!Shell) return;
    Shell.modal.open('lm-cc-modal',
      '<div id="lm-cc-body" style="padding:18px">加载中…</div>',
      { maxWidth: '980px' });
    await _lmRenderCommandCenter();
  };

  // Phase 8d/8g: 过滤器 change handler (供 select / sparkline click 调用)
  window.lmCCSetFilter = async function (kind, val) {
    const f = window._lmCCFilter;
    if (kind === 'days') { f.days = parseInt(val) || 7; f.date = ''; }
    else if (kind === 'actor') f.actor = val || '';
    else if (kind === 'date') f.date = val || '';
    const body = document.getElementById('lm-cc-body');
    if (body) body.innerHTML = '<div style="padding:18px">加载中…</div>';
    await _lmRenderCommandCenter();
  };

  // Phase 8g: sparkline 点 circle → 设 date 过滤重渲染
  window.lmCCDrillDate = async function (date) {
    await window.lmCCSetFilter('date', date);
  };

  async function _lmRenderCommandCenter() {
    const Shell = _shell();
    const body = document.getElementById('lm-cc-body');
    if (!body) return;
    const f = window._lmCCFilter;
    const funnelUrl = '/lead-mesh/funnel?days=' + f.days
      + (f.actor ? '&actor=' + encodeURIComponent(f.actor) : '')
      + (f.date ? '&date=' + encodeURIComponent(f.date) : '');
    try {
      // 时序始终用 days (date 下钻时不展示 sparkline 因为单点无意义)
      const tsUrl = '/lead-mesh/funnel/timeseries?days=' + f.days
        + (f.actor ? '&actor=' + encodeURIComponent(f.actor) : '');
      const [pending, ack, completed, rejected, dead, receivers, funnel, timeseries] = await Promise.all([
        Shell.api.get('/lead-mesh/handoffs?state=pending&limit=500'),
        Shell.api.get('/lead-mesh/handoffs?state=acknowledged&limit=500'),
        Shell.api.get('/lead-mesh/handoffs?state=completed&limit=500'),
        Shell.api.get('/lead-mesh/handoffs?state=rejected&limit=500'),
        Shell.api.get('/lead-mesh/webhooks/dead-letters?limit=100'),
        Shell.api.get('/lead-mesh/receivers?with_load=true&enabled_only=true'),
        Shell.api.get(funnelUrl),
        Shell.api.get(tsUrl),
      ]);
      const pn = (pending.handoffs || []).length;
      const an = (ack.handoffs || []).length;
      const cn = (completed.handoffs || []).length;
      const rn = (rejected.handoffs || []).length;
      const total = pn + an + cn + rn;
      const deadN = (dead.dead_letters || []).length;

      // 接收方负载
      const rvLoad = {};
      [].concat(pending.handoffs || [], ack.handoffs || []).forEach(function (h) {
        const k = h.receiver_account_key || '(未指派)';
        rvLoad[k] = (rvLoad[k] || 0) + 1;
      });
      // 按渠道分组 - 每渠道分别算各 state 数, 转化率 = completed/(total excl. pending)
      // (excluding pending 因为 pending 还没定结果, 算进分母拉低实际转化数据)
      const chStats = { pending: {}, ack: {}, completed: {}, rejected: {} };
      (pending.handoffs || []).forEach(function (h) {
        chStats.pending[h.channel] = (chStats.pending[h.channel] || 0) + 1;
      });
      (ack.handoffs || []).forEach(function (h) {
        chStats.ack[h.channel] = (chStats.ack[h.channel] || 0) + 1;
      });
      (completed.handoffs || []).forEach(function (h) {
        chStats.completed[h.channel] = (chStats.completed[h.channel] || 0) + 1;
      });
      (rejected.handoffs || []).forEach(function (h) {
        chStats.rejected[h.channel] = (chStats.rejected[h.channel] || 0) + 1;
      });

      const funnelBar = function (label, count, color) {
        const pct = total > 0 ? Math.round(count * 100 / total) : 0;
        return ''
          + '<div style="margin-bottom:8px">'
          + '  <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:2px">'
          + '    <span>' + label + '</span>'
          + '    <span style="color:' + color + ';font-weight:600">' + count + ' (' + pct + '%)</span>'
          + '  </div>'
          + '  <div style="height:10px;background:rgba(255,255,255,.05);border-radius:4px;overflow:hidden">'
          + '    <div style="width:' + pct + '%;height:100%;background:' + color + '"></div>'
          + '  </div>'
          + '</div>';
      };

      // 接收方负载 — 优先走 receivers API (含 cap/percent), 回退到 rvLoad 计数
      const rvList = (receivers && receivers.receivers) || [];
      const atRiskReceivers = [];   // 收集 ≥90% 的, 稍后弹 toast
      const rvRows = rvList.length > 0
        ? rvList.map(function (r) {
            const cap = r.cap || r.daily_cap || 0;
            const cur = r.current || 0;
            const pct = cap > 0 ? Math.round(cur * 100 / cap) : 0;
            const barColor = pct >= 90 ? '#ef4444' : pct >= 60 ? '#f59e0b' : '#22c55e';
            const atRisk = pct >= 90;
            if (atRisk) atRiskReceivers.push(r.key + '(' + pct + '%)');
            const rowStyle = atRisk
              ? 'border:1px solid #ef4444;background:rgba(239,68,68,.06);animation:lmPulseRed 2s ease-in-out infinite'
              : 'background:var(--bg-main)';
            const nameHtml = atRisk
              ? '<b style="color:#ef4444">⚠ ' + _safe(r.key) + '</b>'
              : '<code>' + _safe(r.key) + '</code>';
            return ''
              + '<div style="padding:6px 10px;border-radius:4px;margin-bottom:4px;font-size:12px;' + rowStyle + '">'
              + '  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">'
              + '    <span>' + nameHtml
              + '      <span style="color:var(--text-dim);font-size:10px;margin-left:4px">' + _safe(r.channel || '') + '</span></span>'
              + '    <span style="color:' + barColor + ';font-weight:600">' + cur + ' / ' + cap
              + '    <span style="font-size:10px">(' + pct + '%)</span></span>'
              + '  </div>'
              + '  <div style="height:5px;background:rgba(255,255,255,.05);border-radius:3px;overflow:hidden">'
              + '    <div style="width:' + pct + '%;height:100%;background:' + barColor + '"></div>'
              + '  </div>'
              + '</div>';
          }).join('')
        : Object.entries(rvLoad).sort(function (a, b) { return b[1] - a[1]; })
            .map(function (kv) {
              return '<div style="display:flex;justify-content:space-between;padding:6px 10px;background:var(--bg-main);border-radius:4px;margin-bottom:4px;font-size:12px">'
                + '<code>' + _safe(kv[0]) + '</code>'
                + '<span style="color:#f59e0b">' + kv[1] + ' 待/已确认</span></div>';
            }).join('');

      const allChannels = {};
      ['pending', 'ack', 'completed', 'rejected'].forEach(function (s) {
        Object.keys(chStats[s]).forEach(function (ch) { allChannels[ch] = 1; });
      });
      const chRows = Object.keys(allChannels).sort()
        .map(function (ch) {
          const p = chStats.pending[ch] || 0;
          const a = chStats.ack[ch] || 0;
          const c = chStats.completed[ch] || 0;
          const rj = chStats.rejected[ch] || 0;
          // 转化率: completed / (completed + rejected + ack)
          // pending 不算分母 (结果未定), ack 算已投递但未完成
          const resolved = c + rj + a;
          const rate = resolved > 0 ? Math.round(c * 100 / resolved) : 0;
          const rateColor = rate >= 60 ? '#22c55e' : rate >= 30 ? '#f59e0b' : '#ef4444';
          return '<tr>'
            + '<td style="padding:6px 10px"><b>' + _safe(ch) + '</b></td>'
            + '<td style="padding:6px 10px;color:#f59e0b;text-align:right">' + p + '</td>'
            + '<td style="padding:6px 10px;color:#22c55e;text-align:right">' + c + '</td>'
            + '<td style="padding:6px 10px;color:' + rateColor + ';text-align:right;font-weight:600">'
            + (resolved > 0 ? rate + '%' : '-')
            + '<span style="color:var(--text-dim);font-size:10px;font-weight:400"> (' + c + '/' + resolved + ')</span>'
            + '</td></tr>';
        }).join('');

      const deadHtml = deadN === 0
        ? '<div style="color:#22c55e;font-size:12px">✓ 无失败 webhook</div>'
        : '<div style="color:#ef4444;font-size:12px;margin-bottom:6px">⚠ ' + deadN + ' 条死信</div>'
          + '<button onclick="lmViewDeadLetters()" '
          + 'style="padding:5px 12px;background:rgba(239,68,68,.12);color:#ef4444;border:1px solid rgba(239,68,68,.3);border-radius:6px;font-size:11px;cursor:pointer">查看 / 重试</button>';

      // Phase 8b: A 端获客漏斗 (从 /lead-mesh/funnel 拿到)
      const fu = funnel || {};
      const fuTotal = fu.total_extracted || 0;
      const fuFr = fu.total_friend_requested || 0;
      const fuGs = fu.total_greeting_sent || 0;
      const fuGb = fu.total_greeting_blocked || 0;
      const fuRate = Math.round(((fu.rate_greet_after_friend || 0) * 100));
      const fuInline = fu.greeting_via_inline || 0;
      const fuFallback = fu.greeting_via_fallback || 0;
      const fuUnknown = fu.greeting_via_unknown || 0;
      const fuRateColor = fuRate >= 50 ? '#22c55e' : fuRate >= 25 ? '#f59e0b' : '#ef4444';
      const topBlocked = (fu.top_blocked_reason || '').trim();
      // persona 分布 top 3
      const perPersona = fu.per_persona_friend_requested || {};
      const personaEntries = Object.entries(perPersona)
        .sort(function (a, b) { return b[1] - a[1]; }).slice(0, 3);
      const personaHtml = personaEntries.length === 0
        ? '<span style="color:var(--text-dim);font-size:11px">暂无</span>'
        : personaEntries.map(function (kv) {
            return '<span style="display:inline-block;padding:2px 8px;background:rgba(96,165,250,.12);'
              + 'color:#60a5fa;border-radius:10px;font-size:11px;margin-right:6px">'
              + _safe(kv[0]) + ': ' + kv[1] + '</span>';
          }).join('');

      const funnelNumber = function (label, n, color) {
        return '<div style="flex:1;text-align:center">'
          + '  <div style="font-size:22px;font-weight:700;color:' + color + '">' + n + '</div>'
          + '  <div style="font-size:11px;color:var(--text-dim);margin-top:2px">' + label + '</div>'
          + '</div>';
      };

      // Phase 8d 过滤器: days + actor select
      const ff = window._lmCCFilter;
      const daysOpts = [1, 3, 7, 14, 30].map(function (v) {
        return '<option value="' + v + '"'
          + (v === ff.days ? ' selected' : '') + '>'
          + (v === 1 ? '24 小时' : v + ' 天') + '</option>';
      }).join('');
      const actorOpts = [
        { v: 'agent_a', label: 'A 端' },
        { v: 'agent_b', label: 'B 端' },
        { v: '', label: '全部' },
      ].map(function (o) {
        return '<option value="' + o.v + '"'
          + (o.v === ff.actor ? ' selected' : '') + '>'
          + o.label + '</option>';
      }).join('');
      // Phase 8g: date 下钻时显示 chip, 点 × 清除回到 days 窗口
      const dateChipHtml = ff.date
        ? '  <span style="display:inline-flex;align-items:center;gap:4px;'
          + '             padding:3px 8px;background:rgba(245,158,11,.15);'
          + '             color:#f59e0b;border:1px solid rgba(245,158,11,.4);'
          + '             border-radius:12px;font-size:11px">'
          + '    📅 ' + _safe(ff.date)
          + '    <button onclick="lmCCSetFilter(\'date\', \'\')" '
          + '            title="清除单日过滤, 回到 ' + ff.days + ' 天窗口"'
          + '            style="background:none;border:none;color:#f59e0b;'
          + '                   cursor:pointer;padding:0;font-size:13px;line-height:1">✕</button>'
          + '  </span>'
        : '';

      const filterHtml = ''
        + '<div style="display:flex;gap:8px;align-items:center;font-size:11px">'
        + '  <select onchange="lmCCSetFilter(\'days\', this.value)" '
        + '          ' + (ff.date ? 'disabled title="清除 date chip 才能切 days"' : '')
        + '          style="padding:3px 8px;background:var(--bg-main);color:var(--text);'
        + '                 border:1px solid var(--border);border-radius:4px;font-size:11px'
        + (ff.date ? ';opacity:.5' : '') + '">'
        +    daysOpts
        + '  </select>'
        + '  <select onchange="lmCCSetFilter(\'actor\', this.value)" '
        + '          style="padding:3px 8px;background:var(--bg-main);color:var(--text);'
        + '                 border:1px solid var(--border);border-radius:4px;font-size:11px">'
        +    actorOpts
        + '  </select>'
        +    dateChipHtml
        + '</div>';

      // 瓶颈可点击: 点 code 跳 blocked peer 子 modal
      const topBlockedHtml = topBlocked
        ? '    <div><span style="color:var(--text-dim)">瓶颈:</span>'
          + '      <code style="color:#f59e0b;margin-left:4px;cursor:pointer;'
          + '                  text-decoration:underline dotted" '
          + '            onclick="lmOpenBlockedPeers(\'' + _safe(topBlocked) + '\')" '
          + '            title="点击查看具体被挡的 peer">' + _safe(topBlocked) + '</code></div>'
        : '    <div style="color:#22c55e">✓ 无主要瓶颈</div>';

      // Phase 8e: sparkline SVG — 纯 SVG 零依赖
      const series = (timeseries && timeseries.series) || [];
      const sparkHtml = _lmBuildSparkline(series, f.days);

      const aFunnelCard = ''
        + '<div style="grid-column:1/-1;padding:14px;background:rgba(96,165,250,.06);'
        + '            border:1px solid rgba(96,165,250,.25);border-radius:8px">'
        + '  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">'
        + '    <div style="display:flex;gap:12px;align-items:center">'
        + '      <span style="font-size:13px;color:var(--text-muted)">🎯 A 端获客漏斗</span>'
        +        filterHtml
        + '    </div>'
        + '    <div style="font-size:11px">'
        + '      转化率: <b style="color:' + fuRateColor + ';font-size:14px">' + fuRate + '%</b>'
        + '      <span style="color:var(--text-dim)"> (greeting/friend_req)</span>'
        + '    </div>'
        + '  </div>'
        + '  <div style="display:flex;gap:4px;align-items:center">'
        +      funnelNumber('extracted', fuTotal, '#94a3b8')
        + '    <span style="color:var(--text-dim);font-size:20px">→</span>'
        +      funnelNumber('friend_req', fuFr, '#60a5fa')
        + '    <span style="color:var(--text-dim);font-size:20px">→</span>'
        +      funnelNumber('greeting', fuGs, '#22c55e')
        + '    <span style="color:var(--text-dim);font-size:20px">·</span>'
        +      funnelNumber('blocked', fuGb, fuGb > 0 ? '#f59e0b' : '#94a3b8')
        + '  </div>'
        + '  <div style="display:flex;justify-content:space-between;margin-top:12px;'
        + '              padding-top:10px;border-top:1px dashed rgba(255,255,255,.08);font-size:11px">'
        + '    <div>'
        + '      <span style="color:var(--text-dim)">via</span>:'
        + '      <span style="color:#22c55e;margin-left:4px">inline=' + fuInline + '</span>'
        + '      <span style="color:#60a5fa;margin-left:8px">fallback=' + fuFallback + '</span>'
        + (fuUnknown > 0
            ? '      <span style="color:var(--text-dim);margin-left:8px">unknown=' + fuUnknown + '</span>'
            : '')
        + '    </div>'
        +      topBlockedHtml
        + '  </div>'
        + '  <div style="margin-top:10px;font-size:11px">'
        + '    <span style="color:var(--text-dim)">top persona:</span> ' + personaHtml
        + '  </div>'
        +    sparkHtml
        + '</div>';

      body.innerHTML = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '  <div>'
        + '    <div style="font-size:18px;font-weight:700">📊 运营指挥台</div>'
        + '    <div style="font-size:11px;color:var(--text-muted);margin-top:2px">'
        +        '本周总 ' + total + ' 单 · 完成率 ' + (total > 0 ? Math.round(cn * 100 / total) : 0) + '%</div>'
        + '  </div>'
        + '  <button onclick="PlatShell.modal.close(\'lm-cc-modal\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '</div>'
        + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">'
        +      aFunnelCard
        + '  <div>'
        + '    <div style="font-size:13px;color:var(--text-muted);margin-bottom:8px">🔻 交接漏斗</div>'
        +      funnelBar('待处理', pn, _STATE_COLOR.pending)
        +      funnelBar('已确认', an, _STATE_COLOR.acknowledged)
        +      funnelBar('已完成', cn, _STATE_COLOR.completed)
        +      funnelBar('已拒接', rn, _STATE_COLOR.rejected)
        + '  </div>'
        + '  <div>'
        + '    <div style="font-size:13px;color:var(--text-muted);margin-bottom:8px">📊 按渠道</div>'
        + '    <table style="width:100%;font-size:12px">'
        + '      <thead><tr style="color:var(--text-dim)">'
        + '        <th style="text-align:left;padding:6px 10px">渠道</th>'
        + '        <th style="text-align:right;padding:6px 10px">待/确认</th>'
        + '        <th style="text-align:right;padding:6px 10px">完成</th>'
        + '        <th style="text-align:right;padding:6px 10px">转化率</th>'
        + '      </tr></thead><tbody>' + (chRows || '<tr><td colspan="4" style="text-align:center;color:var(--text-dim);padding:14px">暂无数据</td></tr>') + '</tbody>'
        + '    </table>'
        + '  </div>'
        + '  <div>'
        + '    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'
        + '      <span style="font-size:13px;color:var(--text-muted)">📬 接收方负载</span>'
        + (atRiskReceivers.length > 0
            ? '      <span style="font-size:10px;color:#ef4444;font-weight:700">⚠ ' + atRiskReceivers.length + ' 已接近满载</span>'
            : '')
        + '    </div>'
        +      (rvRows || '<div style="color:var(--text-dim);font-size:12px">无接收方或无待处理交接</div>')
        + '  </div>'
        + '  <div>'
        + '    <div style="font-size:13px;color:var(--text-muted);margin-bottom:8px">⚠ Webhook 健康</div>'
        +      deadHtml
        + '    <div style="margin-top:14px">'
        + '      <div style="font-size:13px;color:var(--text-muted);margin-bottom:8px">🔧 运维操作</div>'
        + '      <button onclick="lmFlushWebhooks()" '
        + '              style="padding:6px 12px;background:rgba(96,165,250,.15);color:#60a5fa;border:1px solid rgba(96,165,250,.4);border-radius:6px;font-size:11px;cursor:pointer;margin-right:6px">⚡ 手动 flush webhook</button>'
        + '    </div>'
        + '  </div>'
        + '</div>';

      // 注入一次 keyframes — 让 atRisk 行呼吸红光
      _lmInjectPulseKeyframes();

      // 负载告警 toast — 有 ≥90% 的 receiver 时弹红色警告 (每次打开 dashboard 一次)
      if (atRiskReceivers.length > 0 && typeof showToast === 'function') {
        showToast('⚠ 接收方负载告警: ' + atRiskReceivers.join(', ')
                   + ' 已 ≥90%, 请考虑启用备用或提升 daily_cap',
                   'error');
      }

      // Phase 8b: 获客漏斗瓶颈 toast — 有足够样本 (friend_req ≥ 5) 且
      // 转化率 <25% 或 top_blocked_reason 明显时主动提醒
      if (fuFr >= 5 && typeof showToast === 'function') {
        if (fuRate < 25) {
          showToast('⚠ A 端 greeting 转化率仅 ' + fuRate + '% '
                     + '(' + fuGs + '/' + fuFr + '). '
                     + (topBlocked ? '主要瓶颈: ' + topBlocked : '')
                     + ' 建议检查 profile UI 或 Messenger fallback 配置',
                     'warning');
        } else if (topBlocked === 'messenger_not_installed') {
          showToast('⚠ 瓶颈: messenger_not_installed — '
                     + '多台设备未装 Messenger, fallback 链路无法启用',
                     'warning');
        }
      }
    } catch (e) {
      body.innerHTML = '<div style="color:#ef4444;padding:20px">加载失败: ' + _safe(e.message || e) + '</div>';
    }
  };

  // Phase 8e: sparkline — 3 条线 (friend_req/greeting/blocked) 纯 SVG 零依赖.
  //   series: [{date: "YYYY-MM-DD", friend_req, greeting_sent, blocked}]
  //   days: 时间窗口 (<= 1 时 return ''; 单点无 sparkline 意义)
  function _lmBuildSparkline(series, days) {
    if (!Array.isArray(series) || series.length <= 1 || days <= 1) return '';
    const W = 600, H = 64;
    const PAD_X = 6, PAD_Y = 6;
    const plotW = W - 2 * PAD_X;
    const plotH = H - 2 * PAD_Y;
    const n = series.length;
    const stepX = n > 1 ? plotW / (n - 1) : 0;
    // y 范围: max 向上取整到 5 的倍数 (留 20% 头部空间)
    let ymax = 0;
    series.forEach(function (p) {
      ymax = Math.max(ymax,
        (p.friend_req || 0),
        (p.greeting_sent || 0),
        (p.blocked || 0));
    });
    ymax = Math.max(5, Math.ceil(ymax * 1.2 / 5) * 5);

    const yToPx = function (v) {
      return PAD_Y + plotH - (v / ymax) * plotH;
    };
    const makeLine = function (key, color) {
      const pts = series.map(function (p, i) {
        return (PAD_X + i * stepX).toFixed(1) + ',' + yToPx(p[key] || 0).toFixed(1);
      }).join(' ');
      // Phase 8g: circle 加 onclick → 下钻到单日 (点任意颜色都是同一天)
      const circles = series.map(function (p, i) {
        const cx = (PAD_X + i * stepX).toFixed(1);
        const cy = yToPx(p[key] || 0).toFixed(1);
        const v = p[key] || 0;
        return '<circle cx="' + cx + '" cy="' + cy + '" r="2.5" fill="' + color + '"'
          + ' style="cursor:pointer"'
          + ' onclick="lmCCDrillDate(\'' + p.date + '\')">'
          + '<title>' + p.date + ' ' + key + '=' + v + ' (点击下钻)</title></circle>';
      }).join('');
      return '<polyline fill="none" stroke="' + color
        + '" stroke-width="1.5" points="' + pts + '"/>' + circles;
    };

    const legendItem = function (color, label) {
      return '<span style="display:inline-flex;align-items:center;margin-right:10px">'
        + '  <span style="display:inline-block;width:10px;height:2px;background:' + color
        + ';margin-right:4px"></span>' + label + '</span>';
    };
    return '<div style="margin-top:14px;padding-top:10px;'
      + '              border-top:1px dashed rgba(255,255,255,.08)">'
      + '  <div style="display:flex;justify-content:space-between;align-items:center;'
      + '              margin-bottom:4px;font-size:10px;color:var(--text-dim)">'
      + '    <span>📈 近 ' + days + ' 天每日</span>'
      + '    <span>'
      + legendItem('#60a5fa', 'friend_req')
      + legendItem('#22c55e', 'greeting')
      + legendItem('#f59e0b', 'blocked')
      + '    </span>'
      + '  </div>'
      + '  <svg width="100%" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '" '
      + '       preserveAspectRatio="none" style="display:block">'
      + '    <line x1="' + PAD_X + '" y1="' + yToPx(0)
      + '" x2="' + (W - PAD_X) + '" y2="' + yToPx(0)
      + '" stroke="rgba(255,255,255,.06)" stroke-width="1"/>'
      +      makeLine('friend_req', '#60a5fa')
      +      makeLine('greeting_sent', '#22c55e')
      +      makeLine('blocked', '#f59e0b')
      + '  </svg>'
      + '  <div style="display:flex;justify-content:space-between;font-size:10px;'
      + '              color:var(--text-dim);margin-top:2px">'
      + '    <span>' + _safe(series[0].date.substring(5)) + '</span>'
      + '    <span>' + _safe(series[series.length - 1].date.substring(5)) + '</span>'
      + '  </div>'
      + '</div>';
  }

  function _lmInjectPulseKeyframes() {
    if (document.getElementById('lm-pulse-keyframes')) return;
    const s = document.createElement('style');
    s.id = 'lm-pulse-keyframes';
    s.textContent = '@keyframes lmPulseRed {'
      + ' 0%,100% { box-shadow: 0 0 0 0 rgba(239,68,68,.4); }'
      + ' 50% { box-shadow: 0 0 0 4px rgba(239,68,68,.15); } }';
    document.head.appendChild(s);
  }

  // Phase 8d: 点击漏斗瓶颈看具体被挡 peer 列表
  window.lmOpenBlockedPeers = async function (reason) {
    const Shell = _shell();
    if (!Shell || !reason) return;
    const f = window._lmCCFilter || { days: 7 };
    Shell.modal.open('lm-blocked-peers-modal',
      '<div id="lm-bp-body" style="padding:18px">加载中…</div>',
      { maxWidth: '720px' });
    try {
      const r = await Shell.api.get(
        '/lead-mesh/funnel/blocked-peers?reason=' + encodeURIComponent(reason)
        + '&days=' + f.days + '&limit=50'
        + (f.date ? '&date=' + encodeURIComponent(f.date) : ''));
      const peers = (r && r.peers) || [];
      const rows = peers.length === 0
        ? '<div style="text-align:center;padding:30px;color:var(--text-dim)">✓ 该 reason 下无被挡 peer (时间窗口内)</div>'
        : peers.map(function (p) {
            const cid = p.canonical_id || '';
            const cidShort = cid.substring(0, 8);
            const at = (p.last_blocked_at || '').substring(0, 19);
            const persona = p.persona_key || '';
            return ''
              + '<div style="padding:10px 14px;background:var(--bg-main);'
              + '            border-left:3px solid #f59e0b;border-radius:4px;margin-bottom:6px;'
              + '            display:flex;justify-content:space-between;align-items:center">'
              + '  <div style="flex:1;min-width:0">'
              + '    <div style="font-size:12px">'
              + '      <code style="color:#60a5fa">' + _safe(cidShort) + '…</code>'
              + '      <span style="margin-left:10px;color:var(--text-dim);font-size:10px">'
              +          _safe(at) + '</span>'
              + (persona
                  ? '      <span style="margin-left:10px;padding:1px 6px;background:rgba(96,165,250,.12);'
                    + '                   color:#60a5fa;border-radius:8px;font-size:10px">'
                    + _safe(persona) + '</span>'
                  : '')
              + '    </div>'
              + '    <div style="font-size:10px;color:var(--text-dim);margin-top:2px">'
              + '      被挡次数: <b style="color:#f59e0b">' + p.n_blocked + '</b>'
              + '    </div>'
              + '  </div>'
              + '  <button onclick="PlatShell.modal.close(\'lm-blocked-peers-modal\');'
              + '                   lmOpenLeadDossier(\'' + _safe(cid) + '\')" '
              + '          style="padding:4px 10px;background:rgba(96,165,250,.12);color:#60a5fa;'
              + '                 border:1px solid rgba(96,165,250,.3);border-radius:4px;'
              + '                 font-size:11px;cursor:pointer">📖 dossier</button>'
              + '</div>';
          }).join('');
      document.getElementById('lm-bp-body').innerHTML = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '  <div>'
        + '    <div style="font-size:16px;font-weight:700">🔍 被挡 peer 列表</div>'
        + '    <div style="font-size:11px;color:var(--text-muted);margin-top:2px">'
        + '      reason: <code style="color:#f59e0b">' + _safe(reason) + '</code>'
        + '      · 近 ' + f.days + ' 天 · 共 ' + peers.length + ' 个唯一 peer'
        + '    </div>'
        + '  </div>'
        + '  <button onclick="PlatShell.modal.close(\'lm-blocked-peers-modal\')" '
        + '          style="background:none;border:1px solid var(--border);color:var(--text);'
        + '                 padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '</div>'
        + rows;
    } catch (e) {
      document.getElementById('lm-bp-body').innerHTML =
        '<div style="color:#ef4444;padding:20px">加载失败: ' + _safe(e.message || e) + '</div>';
    }
  };

  window.lmFlushWebhooks = async function () {
    const Shell = _shell();
    if (!Shell) return;
    try {
      const r = await Shell.api.post('/lead-mesh/webhooks/flush?max_batch=100', {});
      const s = (r && r.stats) || {};
      showToast('Flush 完成: delivered=' + (s.delivered || 0) + ' retried=' + (s.retried || 0) + ' dead=' + (s.dead_letter || 0),
                 (s.dead_letter > 0 ? 'warning' : 'success'));
    } catch (e) {
      showToast('flush 失败: ' + (e.message || e), 'error');
    }
  };

  window.lmViewDeadLetters = async function () {
    const Shell = _shell();
    if (!Shell) return;
    Shell.modal.open('lm-dead-modal',
      '<div id="lm-dead-body" style="padding:18px">加载中…</div>',
      { maxWidth: '920px' });
    try {
      const r = await Shell.api.get('/lead-mesh/webhooks/dead-letters?limit=100');
      const list = (r && r.dead_letters) || [];
      const rows = list.length === 0
        ? '<div style="text-align:center;padding:40px;color:#22c55e">✓ 没有死信</div>'
        : list.map(function (d) {
            return ''
              + '<div style="padding:10px 14px;background:var(--bg-main);border-left:3px solid #ef4444;border-radius:6px;margin-bottom:6px">'
              + '  <div style="display:flex;justify-content:space-between;align-items:center">'
              + '    <div style="flex:1;min-width:0">'
              + '      <div style="font-weight:600;font-size:12px">'
              + _safe(d.event_type) + ' <span style="color:var(--text-muted);font-size:10px">→ ' + _safe(d.target_url) + '</span></div>'
              + '      <div style="font-size:10px;color:var(--text-dim);margin-top:2px">' + _safe(d.last_error || '') + '</div>'
              + '    </div>'
              + '    <button onclick="lmRetryDeadLetter(' + d.id + ')" '
              + '            style="padding:4px 10px;background:rgba(34,197,94,.15);color:#22c55e;border:1px solid rgba(34,197,94,.4);border-radius:4px;font-size:11px;cursor:pointer">🔄 重试</button>'
              + '  </div>'
              + '</div>';
          }).join('');
      document.getElementById('lm-dead-body').innerHTML = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '  <div style="font-size:16px;font-weight:700">⚠ Webhook 死信队列 (' + list.length + ')</div>'
        + '  <button onclick="PlatShell.modal.close(\'lm-dead-modal\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '</div>'
        + rows;
    } catch (e) {
      document.getElementById('lm-dead-body').innerHTML = '<div style="color:#ef4444">加载失败</div>';
    }
  };

  window.lmRetryDeadLetter = async function (dispatchId) {
    const Shell = _shell();
    if (!Shell) return;
    try {
      await Shell.api.post('/lead-mesh/webhooks/' + dispatchId + '/retry', {});
      showToast('已重置为 pending, 下次 flush 会重试', 'success');
      lmViewDeadLetters();
    } catch (e) {
      showToast('重置失败: ' + (e.message || e), 'error');
    }
  };


  // ─────────────────────────────────────────────────────────────────
  // P1 · 接收方账号管理 (Phase 6.B, 2026-04-23)
  // ─────────────────────────────────────────────────────────────────

  window.lmOpenReceiversConfig = async function () {
    const Shell = _shell();
    if (!Shell) return;
    Shell.modal.open('lm-receivers-modal',
      '<div id="lm-receivers-body" style="padding:18px">加载中…</div>',
      { maxWidth: '1000px' });
    await _lmRenderReceivers();
  };

  async function _lmRenderReceivers() {
    const Shell = _shell();
    const body = document.getElementById('lm-receivers-body');
    if (!body) return;
    try {
      const r = await Shell.api.get('/lead-mesh/receivers?with_load=true');
      const list = (r && r.receivers) || [];
      _lmInjectPulseKeyframes();
      // 计算负载告警 banner
      const atRisk = list.filter(function (x) {
        const cap = x.cap || x.daily_cap || 0;
        const cur = x.current || 0;
        const pct = cap > 0 ? Math.round(cur * 100 / cap) : 0;
        return x.enabled !== false && pct >= 90;
      });
      const alertBanner = atRisk.length === 0
        ? ''
        : ('<div style="background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.35);'
            + 'border-left:4px solid #ef4444;padding:8px 12px;margin-bottom:12px;border-radius:4px;'
            + 'font-size:12px;color:#ef4444">'
            + '⚠ <b>' + atRisk.length + ' 个接收方负载 ≥ 90%</b>: '
            + atRisk.map(function (x) { return _safe(x.key); }).join(', ')
            + ' — 建议启用 backup_key 或上调 daily_cap'
            + '</div>');
      const rows = list.length === 0
        ? '<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--text-dim)">'
          + '尚无接收方。参考 <code>config/referral_receivers.yaml.example</code> 创建。'
          + '</td></tr>'
        : list.map(_lmReceiverRowHtml).join('');
      body.innerHTML = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '  <div>'
        + '    <div style="font-size:18px;font-weight:700">📬 接收方账号管理</div>'
        + '    <div style="font-size:11px;color:var(--text-muted);margin-top:2px">'
        + '      每个 receiver 是一个接收引流的账号(LINE/WA/TG/IG/Messenger),'
        + ' handoff 自动按 channel + persona + 剩余 cap 路由</div>'
        + '  </div>'
        + '  <div style="display:flex;gap:8px">'
        + '    <button onclick="lmOpenNewReceiverDialog()" '
        + '            style="padding:6px 14px;background:#22c55e;color:#fff;border:none;border-radius:6px;font-size:12px;cursor:pointer">'
        + '      ➕ 新增接收方</button>'
        + '    <button onclick="_lmRenderReceivers()" '
        + '            style="padding:6px 12px;background:rgba(96,165,250,.15);color:#60a5fa;border:1px solid rgba(96,165,250,.4);border-radius:6px;font-size:11px;cursor:pointer">'
        + '      🔄 刷新</button>'
        + '    <button onclick="PlatShell.modal.close(\'lm-receivers-modal\')" '
        + '            style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '  </div>'
        + '</div>'
        + alertBanner
        + '<table style="width:100%;border-collapse:collapse;font-size:12px">'
        + '  <thead><tr style="color:var(--text-dim);background:rgba(255,255,255,.03)">'
        + '    <th style="text-align:left;padding:8px">Key</th>'
        + '    <th style="text-align:left;padding:8px">渠道</th>'
        + '    <th style="text-align:left;padding:8px">账号(脱敏)</th>'
        + '    <th style="text-align:left;padding:8px">今日负载</th>'
        + '    <th style="text-align:left;padding:8px">备用</th>'
        + '    <th style="text-align:left;padding:8px">状态</th>'
        + '    <th style="text-align:left;padding:8px">操作</th>'
        + '  </tr></thead><tbody>' + rows + '</tbody>'
        + '</table>'
        + '<div style="margin-top:14px;font-size:11px;color:var(--text-dim)">'
        + '  💡 配置文件: <code>config/referral_receivers.yaml</code>(热加载);'
        + ' 轮转算法 least_loaded; at_cap 时自动跳 backup_key'
        + '</div>';
    } catch (e) {
      body.innerHTML = '<div style="color:#ef4444;padding:20px">加载失败: ' + _safe(e.message || e) + '</div>';
    }
  }

  function _lmReceiverRowHtml(r) {
    const enabled = r.enabled !== false;
    const cap = r.cap || r.daily_cap || 0;
    const cur = r.current || 0;
    const pct = cap > 0 ? Math.round(cur * 100 / cap) : 0;
    const barColor = pct >= 90 ? '#ef4444' : pct >= 60 ? '#f59e0b' : '#22c55e';
    // 接近/已满: 红字 + 行脉冲发光, 引导 ops 立即处置
    const atRisk = enabled && pct >= 90;
    const rowExtraStyle = atRisk
      ? ';background:rgba(239,68,68,.06);animation:lmPulseRed 2s ease-in-out infinite'
      : '';
    const pctLabel = atRisk
      ? '<span style="color:#ef4444;font-weight:700">⚠ ' + pct + '%</span>'
      : '<span style="color:' + barColor + '">' + pct + '%</span>';
    const statusBadge = enabled
      ? '<span style="color:#22c55e;font-weight:600">● 启用</span>'
      : '<span style="color:#94a3b8">○ 禁用</span>';
    const toggleBtn = enabled
      ? ('<button onclick="lmToggleReceiver(\'' + _safe(r.key) + '\', false)" '
         + 'style="padding:3px 8px;font-size:11px;background:rgba(245,158,11,.12);color:#f59e0b;border:1px solid rgba(245,158,11,.3);border-radius:4px;cursor:pointer">禁用</button>')
      : ('<button onclick="lmToggleReceiver(\'' + _safe(r.key) + '\', true)" '
         + 'style="padding:3px 8px;font-size:11px;background:rgba(34,197,94,.12);color:#22c55e;border:1px solid rgba(34,197,94,.3);border-radius:4px;cursor:pointer">启用</button>');
    const personaTags = (r.persona_filter || []).slice(0, 2).join(', ') || '所有';

    return ''
      + '<tr style="border-bottom:1px solid var(--border)' + rowExtraStyle + '">'
      + '  <td style="padding:8px;cursor:pointer" onclick="lmOpenEditReceiver(\'' + _safe(r.key) + '\')"'
      + '      title="点击编辑">'
      + '    <b style="color:#60a5fa;text-decoration:underline">' + _safe(r.key) + '</b>'
      + '    <div style="font-size:10px;color:var(--text-dim)">' + _safe(r.display_name || '') + '</div>'
      + '    <div style="font-size:10px;color:var(--text-dim)">persona: ' + _safe(personaTags) + '</div>'
      + '  </td>'
      + '  <td style="padding:8px;text-transform:uppercase">' + _safe(r.channel) + '</td>'
      + '  <td style="padding:8px;font-family:monospace">' + _safe(r.account_id_masked || r.account_id || '') + '</td>'
      + '  <td style="padding:8px;min-width:140px">'
      + '    <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:2px">'
      + '      <span>' + cur + ' / ' + cap + '</span>'
      + '      ' + pctLabel
      + '    </div>'
      + '    <div style="height:6px;background:rgba(255,255,255,.05);border-radius:3px;overflow:hidden">'
      + '      <div style="width:' + pct + '%;height:100%;background:' + barColor + '"></div>'
      + '    </div>'
      + '  </td>'
      + '  <td style="padding:8px"><code>' + _safe(r.backup_key || '—') + '</code></td>'
      + '  <td style="padding:8px">' + statusBadge + '</td>'
      + '  <td style="padding:8px">'
      + '    <button onclick="lmOpenEditReceiver(\'' + _safe(r.key) + '\')" '
      + '            style="padding:3px 8px;font-size:11px;background:rgba(14,165,233,.12);color:#0ea5e9;border:1px solid rgba(14,165,233,.3);border-radius:4px;cursor:pointer;margin-right:4px">✏️ 编辑</button>'
      + toggleBtn
      + '    <button onclick="lmDeleteReceiver(\'' + _safe(r.key) + '\')" '
      + '            style="margin-left:4px;padding:3px 8px;font-size:11px;background:rgba(239,68,68,.12);color:#ef4444;border:1px solid rgba(239,68,68,.3);border-radius:4px;cursor:pointer">🗑</button>'
      + '  </td>'
      + '</tr>';
  }

  window.lmToggleReceiver = async function (key, enabled) {
    const Shell = _shell();
    if (!Shell) return;
    try {
      await Shell.api.post('/lead-mesh/receivers/' + encodeURIComponent(key),
                            { enabled: enabled });
      showToast((enabled ? '启用' : '禁用') + ' ' + key + ' 成功', 'success');
      _lmRenderReceivers();
    } catch (e) {
      showToast('切换失败: ' + (e.message || e), 'error');
    }
  };

  window.lmDeleteReceiver = async function (key) {
    const Shell = _shell();
    if (!Shell) return;
    if (!confirm('删除接收方 ' + key + ' ? 已入账的 handoff 不会受影响, 但无法继续路由新 handoff 到该账号。')) return;
    try {
      await Shell.api.delete('/lead-mesh/receivers/' + encodeURIComponent(key));
      showToast('已删除', 'success');
      _lmRenderReceivers();
    } catch (e) {
      showToast('删除失败: ' + (e.message || e), 'error');
    }
  };

  // ─── 表单 HTML 共用工厂 (new / edit 模式复用) ───────────────────────
  function _lmReceiverFormHtml(mode, r) {
    const isEdit = mode === 'edit';
    r = r || {};
    const title = isEdit ? '✏️ 编辑接收方' : '➕ 新增接收方';
    const btnLabel = isEdit ? '保存' : '创建';
    const btnColor = isEdit ? '#0ea5e9' : '#22c55e';
    const keyReadonly = isEdit ? 'readonly disabled style="opacity:0.6;cursor:not-allowed"' : '';
    const keyValue = _safe(r.key || '');
    const channels = ['line', 'whatsapp', 'telegram', 'messenger', 'instagram'];
    const channelOpts = channels.map(function (ch) {
      const selected = r.channel === ch ? ' selected' : '';
      return '<option value="' + ch + '"' + selected + '>' + ch.toUpperCase() + '</option>';
    }).join('');
    const personaVal = (r.persona_filter || []).join(', ');
    const enabledChecked = r.enabled !== false ? 'checked' : '';

    return ''
      + '<div style="padding:18px">'
      + '  <div style="font-size:16px;font-weight:700;margin-bottom:14px">' + title + '</div>'
      + '  <div style="display:grid;grid-template-columns:1fr 2fr;gap:8px;font-size:12px">'
      + '    <label style="align-self:center">Key *:</label>'
      + '    <input id="lm-rf-key" placeholder="line_jp_01" value="' + keyValue + '" ' + keyReadonly
      + '       style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      + '    <label style="align-self:center">渠道 *:</label>'
      + '    <select id="lm-rf-channel" style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      +        channelOpts
      + '    </select>'
      + '    <label style="align-self:center">账号 ID *:</label>'
      + '    <input id="lm-rf-account" placeholder="@jpline01 / +8190... / @username" value="' + _safe(r.account_id || '')
      + '       " style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      + '    <label style="align-self:center">显示名:</label>'
      + '    <input id="lm-rf-display" placeholder="主号 / 首选 LINE" value="' + _safe(r.display_name || '')
      + '       " style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      + '    <label style="align-self:center">日上限:</label>'
      + '    <input id="lm-rf-cap" type="number" value="' + (r.daily_cap || 15)
      + '       " min="0" max="500" style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      + '    <label style="align-self:center">备用 key:</label>'
      + '    <input id="lm-rf-backup" placeholder="(可空) 配额满时转路由到此 key" value="' + _safe(r.backup_key || '')
      + '       " style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      + '    <label style="align-self:center">persona 过滤:</label>'
      + '    <input id="lm-rf-persona" placeholder="(可空,逗号分隔) jp_female_midlife" value="' + _safe(personaVal)
      + '       " style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      + '    <label style="align-self:center">tags:</label>'
      + '    <input id="lm-rf-tags" placeholder="(可空,逗号分隔) primary, japan" value="' + _safe((r.tags || []).join(', '))
      + '       " style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      + '    <label style="align-self:center">Webhook URL:</label>'
      + '    <input id="lm-rf-webhook" placeholder="(可空) receiver 专属 webhook" value="' + _safe(r.webhook_url || '')
      + '       " style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      + '    <label style="align-self:center">启用:</label>'
      + '    <div style="display:flex;align-items:center;gap:6px"><input id="lm-rf-enabled" type="checkbox" ' + enabledChecked
      + '       style="width:16px;height:16px;cursor:pointer"><span style="font-size:11px;color:var(--text-dim)">勾选即启用,不勾选则不接收新 handoff</span></div>'
      + '  </div>'
      + (isEdit
          ? ('<div style="margin-top:10px;padding:8px 12px;background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.3);border-radius:6px;font-size:11px;color:#fbbf24">'
             + '⚠ 修改 account_id 会影响现有 handoff 的路由, 但已入账的 handoff.receiver_account_key 不会跟随变化</div>')
          : '')
      + '  <div style="margin-top:14px;display:flex;gap:8px;justify-content:flex-end">'
      + '    <button onclick="PlatShell.modal.close(\'lm-receiver-form\')" style="padding:6px 14px;background:none;border:1px solid var(--border);color:var(--text);border-radius:6px;cursor:pointer">取消</button>'
      + '    <button onclick="lmSubmitReceiverForm(\'' + mode + '\')" style="padding:6px 14px;background:' + btnColor
      + '       ;color:#fff;border:none;border-radius:6px;font-weight:600;cursor:pointer">' + btnLabel + '</button>'
      + '  </div>'
      + '</div>';
  }

  window.lmOpenNewReceiverDialog = function () {
    const Shell = _shell();
    if (!Shell) return;
    Shell.modal.open('lm-receiver-form',
      _lmReceiverFormHtml('new', {}),
      { maxWidth: '620px' });
    setTimeout(function () {
      const el = document.getElementById('lm-rf-key');
      if (el) el.focus();
    }, 100);
  };

  window.lmOpenEditReceiver = async function (key) {
    const Shell = _shell();
    if (!Shell) return;
    try {
      const r = await Shell.api.get('/lead-mesh/receivers/' + encodeURIComponent(key));
      Shell.modal.open('lm-receiver-form',
        _lmReceiverFormHtml('edit', r),
        { maxWidth: '620px' });
    } catch (e) {
      showToast('加载失败: ' + (e.message || e), 'error');
    }
  };

  // ─── Phase 6 UX: 时间轴按天分组 ─────────────────────────────
  function _lmDayBucket(atIso) {
    // 输入格式 "YYYY-MM-DD HH:MM:SS" 或 ISO; 提取日期部分并按本地时区比较
    const ymd = (atIso || '').substring(0, 10);   // "2026-04-23"
    if (!ymd) return 'unknown';
    const today = new Date();
    const t0 = new Date(today.getFullYear(), today.getMonth(), today.getDate());
    const evtDate = new Date(ymd + 'T00:00:00');
    const diffDays = Math.round((t0 - evtDate) / 86400000);
    if (diffDays === 0) return '今天 · ' + ymd;
    if (diffDays === 1) return '昨天 · ' + ymd;
    if (diffDays < 7) return diffDays + ' 天前 · ' + ymd;
    if (diffDays < 30) return Math.round(diffDays / 7) + ' 周前 · ' + ymd;
    return Math.round(diffDays / 30) + ' 个月前 · ' + ymd;
  }

  function _lmTimelineHtml(journey) {
    if (!journey || !journey.length) {
      return '<div style="text-align:center;padding:20px;color:var(--text-dim)">无事件</div>';
    }
    // 按天分桶 (保持倒序 - 最新在上)
    const buckets = [];   // [{label, events[]}, ...]
    let currentLabel = null;
    const reversed = journey.slice().reverse();  // 最新在上
    reversed.forEach(function (ev) {
      const lbl = _lmDayBucket(ev.at || '');
      if (lbl !== currentLabel) {
        buckets.push({ label: lbl, events: [] });
        currentLabel = lbl;
      }
      buckets[buckets.length - 1].events.push(ev);
    });

    return buckets.map(function (b) {
      const eventsHtml = b.events.map(function (ev) {
        const icon = _ACTION_ICON[ev.action] || '•';
        const color = _ACTION_COLOR[ev.action] || '#94a3b8';
        const actor = ev.actor || '';
        const actorColor = actor.startsWith('agent_a') ? '#22c55e'
                         : actor.startsWith('agent_b') ? '#a855f7'
                         : actor.startsWith('human') ? '#0ea5e9'
                         : '#64748b';
        const actorBadge = actor.startsWith('agent_a') ? '🟢 A'
                         : actor.startsWith('agent_b') ? '🟣 B'
                         : actor.startsWith('human') ? '👤 人'
                         : '⚙️ 系统';
        const dataKeys = ev.data ? Object.keys(ev.data) : [];
        const dataStr = dataKeys.length
          ? ' <details style="display:inline-block;vertical-align:middle"><summary style="cursor:pointer;color:var(--text-dim);font-size:10px">' + dataKeys.length + ' 字段</summary>'
            + '<pre style="margin:4px 0 0 0;padding:6px 8px;background:var(--bg-card);border-radius:4px;font-size:10px;max-width:500px;overflow:auto">' + _safe(JSON.stringify(ev.data, null, 2)) + '</pre></details>'
          : '';
        return ''
          + '<div style="display:flex;gap:10px;padding:6px 0;border-bottom:1px dashed rgba(255,255,255,.05)">'
          + '  <div style="min-width:56px;color:var(--text-dim);font-size:10px;font-family:monospace;padding-top:2px">'
          +      _safe((ev.at || '').substring(11, 19)) + '</div>'
          + '  <div style="font-size:14px">' + icon + '</div>'
          + '  <div style="flex:1;min-width:0">'
          + '    <div style="font-weight:600;color:' + color + ';font-size:12px">' + _safe(ev.action)
          +       ' <span style="font-size:10px;color:' + actorColor + ';font-weight:400;margin-left:6px">' + actorBadge + '</span>'
          + '    </div>'
          + '    <div style="font-size:10px;color:var(--text-muted)">'
          + '      <span style="color:' + actorColor + '">' + _safe(actor) + '</span>'
          +        (ev.actor_device ? ' @<code>' + _safe(ev.actor_device.substring(0, 8)) + '</code>' : '')
          +        (ev.platform ? ' · ' + _safe(ev.platform) : '')
          +        dataStr
          + '    </div>'
          + '  </div>'
          + '</div>';
      }).join('');
      return ''
        + '<div style="margin-bottom:14px">'
        + '  <div style="font-size:11px;color:var(--text-dim);font-weight:600;margin-bottom:4px;padding:4px 8px;background:rgba(255,255,255,.03);border-left:3px solid #60a5fa;border-radius:0 4px 4px 0">'
        + '    📅 ' + _safe(b.label) + ' <span style="color:var(--text-dim);font-weight:400">(' + b.events.length + ' 事件)</span>'
        + '  </div>'
        +    eventsHtml
        + '</div>';
    }).join('');
  }

  window.lmSubmitReceiverForm = async function (mode) {
    const Shell = _shell();
    if (!Shell) return;
    const key = (document.getElementById('lm-rf-key') || {}).value || '';
    if (!key) { showToast('Key 必填', 'warning'); return; }
    const body = {
      channel: (document.getElementById('lm-rf-channel') || {}).value,
      account_id: (document.getElementById('lm-rf-account') || {}).value,
      display_name: (document.getElementById('lm-rf-display') || {}).value,
      daily_cap: parseInt((document.getElementById('lm-rf-cap') || {}).value) || 15,
      backup_key: (document.getElementById('lm-rf-backup') || {}).value || null,
      persona_filter: ((document.getElementById('lm-rf-persona') || {}).value || '')
          .split(',').map(function (s) { return s.trim(); }).filter(Boolean),
      tags: ((document.getElementById('lm-rf-tags') || {}).value || '')
          .split(',').map(function (s) { return s.trim(); }).filter(Boolean),
      webhook_url: (document.getElementById('lm-rf-webhook') || {}).value || '',
      enabled: !!((document.getElementById('lm-rf-enabled') || {}).checked),
    };
    if (mode === 'new' && (!body.channel || !body.account_id)) {
      showToast('渠道和账号 ID 必填', 'warning');
      return;
    }
    try {
      await Shell.api.post('/lead-mesh/receivers/' + encodeURIComponent(key), body);
      showToast((mode === 'edit' ? '保存' : '创建') + ' 成功', 'success');
      PlatShell.modal.close('lm-receiver-form');
      _lmRenderReceivers();
    } catch (e) {
      showToast((mode === 'edit' ? '保存' : '创建') + ' 失败: ' + (e.message || e), 'error');
    }
  };

})();
