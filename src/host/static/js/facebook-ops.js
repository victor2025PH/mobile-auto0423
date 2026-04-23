/* facebook-ops.js — Facebook 平台专属面板逻辑
 *
 * 职责:
 *   - 加载 5 套执行方案预设(从 GET /facebook/presets)
 *   - 渲染设备网格上方的"执行方案"指挥栏(指挥栏 + 引流账号 + 批量发请求)
 *   - 「配置执行流程」模态框(点设备卡片 → 选预设 → 一键启动)
 *   - 引流账号配置弹窗(默认 WA 优先)
 *
 * 与 tiktok-ops.js 的关系:
 *   - 风格、命名、调用方式尽量对齐
 *   - 但 API 路径用 /facebook/* 命名空间,DOM ID 用 fb-* 前缀,避免冲突
 *
 * 与 platform-grid.js 的关系:
 *   - platform-grid.js 提供通用设备网格(已工作)
 *   - facebook-ops.js 在网格之上增加专属指挥栏 + 弹窗
 */

(function () {
  'use strict';

  // ════════════════════════════════════════════════════════
  // 缓存
  // ════════════════════════════════════════════════════════
  let _fbPresets = null;
  let _fbReferrals = null;
  let _fbReferralPriority = null;   // P2-UI Sprint：从 /referral-config 拿
  let _fbActivePersona = null;      // P2-UI Sprint：当前目标客群展示包
  let _fbAvailablePersonas = null;  // 下拉可选客群列表
  let _fbPersonaYamlDefault = null; // GET /active-persona 的 yaml_default_key
  let _fbPersonaOverrideKey = null;   // 运行时 override（若有）
  let _fbInited = false;

  // ════════════════════════════════════════════════════════
  // 公共入口 — 由 overview.js 在导航到 facebook 页时调用
  // ════════════════════════════════════════════════════════
  window.loadFbOpsPanel = async function () {
    try {
      // persona 必须先于其他加载：它决定引流排序、模态默认 GEO 等
      await _fbLoadActivePersona();
      await Promise.all([_fbLoadPresets(), _fbLoadReferrals()]);
      _fbRenderCommandBar();
      if (typeof loadPlatGridPage === 'function') {
        loadPlatGridPage('facebook');
      }
      _fbInited = true;
    } catch (e) {
      console.warn('[facebook-ops] init failed', e);
    }
  };

  // ════════════════════════════════════════════════════════
  // 数据加载
  // ════════════════════════════════════════════════════════
  async function _fbLoadPresets(force) {
    if (_fbPresets && !force) return _fbPresets;
    try {
      const r = await api('GET', '/facebook/presets');
      _fbPresets = (r && r.presets) || [];
    } catch (e) {
      _fbPresets = [];
    }
    return _fbPresets;
  }

  async function _fbLoadReferrals() {
    try {
      const r = await api('GET', '/facebook/referral-config');
      _fbReferrals = (r && r.referrals) || {};
      _fbReferralPriority = (r && r.priority_order) || null;
      if (r && r.persona) _fbActivePersona = r.persona;
    } catch (e) {
      _fbReferrals = {};
    }
    return _fbReferrals;
  }

  // P2-UI Sprint：加载目标客群（persona）展示包
  // 失败时用最小内置回退（全球默认），保证 UI 永远能渲染出来。
  async function _fbLoadActivePersona(force) {
    if (_fbActivePersona && !force) return _fbActivePersona;
    try {
      const r = await api('GET', '/facebook/active-persona');
      _fbActivePersona = (r && r.active) || null;
      _fbAvailablePersonas = (r && r.available) || [];
      _fbPersonaYamlDefault = (r && r.yaml_default_key) || null;
      _fbPersonaOverrideKey = (r && r.override_key) || null;
    } catch (e) {
      _fbActivePersona = null;
      _fbAvailablePersonas = [];
    }
    if (!_fbActivePersona) {
      _fbActivePersona = {
        persona_key: '',
        display_flag: '🌐',
        display_label: '🌐 未配置客群',
        short_label: '未配置',
        country_code: '',
        language: 'en',
        referral_priority: ['whatsapp', 'telegram', 'instagram', 'line'],
        interest_topics: [],
        seed_group_keywords: [],
      };
    }
    if (!_fbAvailablePersonas || !_fbAvailablePersonas.length) {
      _fbAvailablePersonas = [_fbActivePersona];
    }
    return _fbActivePersona;
  }

  // 渠道元数据（图标/中文名/输入占位符/校验提示）
  // 与后端 fb_target_personas.referral_priority 的 4 个枚举值严格对齐。
  const _FB_CHANNEL_META = {
    line:      { icon: '💚', zh: 'LINE',      placeholder: '@xxxxx 或 https://line.me/...', note: '日本/泰国主力' },
    whatsapp:  { icon: '💬', zh: 'WhatsApp',  placeholder: '+81xxxxxxxxxx',               note: '欧美/东南亚主力' },
    instagram: { icon: '📷', zh: 'Instagram', placeholder: '@username',                   note: '全球年轻女性' },
    telegram:  { icon: '✈️', zh: 'Telegram',  placeholder: '@username',                   note: '技术/加密圈' },
  };

  // ════════════════════════════════════════════════════════
  // 顶部指挥栏 — 渲染到 #fb-cmd-bar(若存在)
  // 退化: 如果模板里没有 #fb-cmd-bar,自动注入到 .plat-page 顶部
  // ════════════════════════════════════════════════════════
  function _fbRenderCommandBar() {
    const page = document.querySelector('#page-plat-facebook .plat-page');
    if (!page) return;

    let bar = document.getElementById('fb-cmd-bar');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'fb-cmd-bar';
      bar.style.cssText = 'background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:10px 14px;margin-bottom:12px;display:flex;flex-wrap:wrap;align-items:center;gap:10px';
      page.insertBefore(bar, page.firstChild);
    }

    // P2-UI Sprint：
    //   * 顶部从 8 并列按钮 → 3 分组（流程 / 数据 / 配置），
    //     每组之间用竖线分隔，视觉层级更清晰。
    //   * 删除「批量发请求」按钮（它只是 fbOpenPresetsModal 的别名，
    //     和「配置执行流程」功能完全重复）。
    //   * 左侧显示当前目标客群（persona）徽章，一眼看出正在为哪个
    //     人群下发任务；点击徽章切换客群。
    const persona = _fbActivePersona || {
      display_flag: '🌐', short_label: '未配置', display_label: '未配置客群'
    };
    const personaBadge = `
      <span class="qa-btn" onclick="fbOpenPersonaPicker()" title="${persona.display_label}"
        style="cursor:pointer;display:inline-flex;align-items:center;gap:6px;font-size:11px;
        padding:4px 10px;background:rgba(236,72,153,.15);color:#f472b6;border:1px solid rgba(236,72,153,.3);
        border-radius:6px;font-weight:600">
        <span style="font-size:14px">${persona.display_flag}</span>
        <span>目标客群:${persona.short_label}</span>
        <span style="font-size:10px;opacity:.7">▾</span>
      </span>`;

    const groupDivider = '<span style="width:1px;height:20px;background:var(--border);margin:0 4px"></span>';

    bar.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span style="font-size:11px;padding:4px 10px;background:rgba(24,119,242,.15);color:#60a5fa;border-radius:6px;font-weight:600">
          📘 Facebook 指挥台
        </span>
        ${personaBadge}
      </div>
      <div style="margin-left:auto;display:flex;gap:6px;flex-wrap:wrap;align-items:center">

        <!-- ① 流程：任务下发（最核心，主色强调） -->
        <button class="qa-btn" onclick="fbOpenPresetsModal()" style="background:linear-gradient(135deg,#1877f2,#0d6efd);color:#fff;border:none;font-weight:600;padding:6px 14px;font-size:12px">
          ⚡ 配置执行流程
        </button>

        ${groupDivider}

        <!-- ② 数据：漏斗/风控/线索/画像/日报 —— 只看不改的诊断类 -->
        <button class="qa-btn" onclick="fbOpenFunnelModal()" style="padding:6px 10px;font-size:12px;background:rgba(34,197,94,.15);color:#22c55e">
          📊 漏斗
        </button>
        <button class="qa-btn" onclick="fbOpenRiskModal()" style="padding:6px 10px;font-size:12px;background:rgba(239,68,68,.15);color:#ef4444">
          🛡️ 风控
        </button>
        <button class="qa-btn" onclick="fbOpenLeadsModal()" style="padding:6px 10px;font-size:12px;background:rgba(59,130,246,.15);color:#3b82f6">
          🎯 高分线索
        </button>
        <button class="qa-btn" onclick="fbOpenInsightsModal()" style="padding:6px 10px;font-size:12px;background:rgba(139,92,246,.18);color:#a78bfa">
          🧠 画像识别
        </button>
        <button class="qa-btn" onclick="fbOpenDailyBriefModal()" style="padding:6px 10px;font-size:12px;background:rgba(168,85,247,.15);color:#a855f7">
          📰 AI 日报
        </button>

        ${groupDivider}

        <!-- ③ 配置：引流账号/文案（低频改动） -->
        <button class="qa-btn" onclick="fbOpenReferralModal()" style="padding:6px 10px;font-size:12px">
          🔗 引流账号
        </button>
      </div>
    `;
  }

  // ════════════════════════════════════════════════════════
  // P2-4 Sprint B: 画像识别 Dashboard（读 /facebook/insights/stats）
  // ════════════════════════════════════════════════════════
  window.fbOpenInsightsModal = async function (hours) {
    const h = hours || 24;
    const overlay = _fbModalOverlay('fb-insights-modal');
    overlay.innerHTML = `
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:20px;max-width:920px;width:95%;max-height:86vh;overflow-y:auto">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
          <div>
            <div style="font-size:18px;font-weight:700">🧠 画像识别 Dashboard</div>
            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">最近 <span id="fb-ins-hours">${h}</span> 小时 L1/L2/命中 + 分画像/分设备/成本</div>
          </div>
          <div style="display:flex;gap:6px;align-items:center">
            <select id="fb-ins-hours-sel" onchange="fbOpenInsightsModal(parseInt(this.value))" style="background:var(--bg-main);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:4px 8px;font-size:12px">
              <option value="1" ${h===1?'selected':''}>1 小时</option>
              <option value="6" ${h===6?'selected':''}>6 小时</option>
              <option value="24" ${h===24?'selected':''}>24 小时</option>
              <option value="168" ${h===168?'selected':''}>7 天</option>
            </select>
            <button onclick="document.getElementById('fb-insights-modal').remove()" style="background:none;border:none;color:var(--text-muted);font-size:22px;cursor:pointer">✕</button>
          </div>
        </div>
        <div id="fb-ins-body" style="font-size:13px;color:var(--text-dim)">加载中…</div>
      </div>
    `;
    try {
      const r = await api('GET', `/facebook/insights/stats?hours=${h}`);
      const body = document.getElementById('fb-ins-body');
      if (!body) return;
      if (!r || !r.ok) { body.innerHTML = '<div style="color:#ef4444">接口返回异常</div>'; return; }
      const t = r.totals || {};
      const kpi = [
        ['L1 扫描总数', (t.l1 || 0), '#60a5fa'],
        ['L2 深判总数', (t.l2 || 0), '#8b5cf6'],
        ['命中', (t.matched || 0), '#22c55e'],
        ['L1→L2 转化率', ((t.l1_to_l2_rate || 0) * 100).toFixed(1) + '%', '#f59e0b'],
        ['L2 命中率', ((t.l2_match_rate || 0) * 100).toFixed(1) + '%', '#22c55e'],
        ['L2 平均耗时', (t.avg_l2_latency_ms || 0) + ' ms', '#94a3b8'],
      ].map(([lbl, v, c]) => `
        <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:10px 12px">
          <div style="color:var(--text-dim);font-size:10px">${lbl}</div>
          <div style="font-weight:700;font-size:18px;color:${c};margin-top:4px">${v}</div>
        </div>
      `).join('');
      const byPersona = (r.by_persona || []).map(p => `
        <tr><td style="padding:6px 10px">${p.persona_key}</td><td style="text-align:right;padding:6px 10px">${p.l1}</td><td style="text-align:right;padding:6px 10px">${p.l2}</td><td style="text-align:right;padding:6px 10px;color:#22c55e;font-weight:600">${p.matched}</td></tr>
      `).join('') || '<tr><td colspan="4" style="padding:8px;color:var(--text-muted);text-align:center">暂无数据</td></tr>';
      const byDev = (r.top_devices || []).slice(0, 10).map(d => {
        const alias = (typeof ALIAS !== 'undefined' ? (ALIAS[d.device_id] || d.device_id.substring(0, 8)) : d.device_id.substring(0, 8));
        return `<tr><td style="padding:6px 10px">${alias}</td><td style="text-align:right;padding:6px 10px">${d.l1}</td><td style="text-align:right;padding:6px 10px">${d.l2}</td><td style="text-align:right;padding:6px 10px;color:#22c55e">${d.matched}</td></tr>`;
      }).join('') || '<tr><td colspan="4" style="padding:8px;color:var(--text-muted);text-align:center">暂无设备数据</td></tr>';
      const costRows = (r.ai_cost || []).map(c => {
        const waitStr = (c.avg_queue_wait_ms != null)
          ? `<span title="本窗口平均排队 / 峰值" style="color:${c.avg_queue_wait_ms>1000?'#f59e0b':'var(--text-dim)'}">${c.avg_queue_wait_ms}ms / ${c.peak_queue_wait_ms}ms</span>`
          : '<span style="color:var(--text-muted)">—</span>';
        return `<tr><td style="padding:6px 10px">${c.provider}/${c.model}</td><td style="padding:6px 10px">${c.scene || '—'}</td><td style="text-align:right;padding:6px 10px">${c.count}</td><td style="text-align:right;padding:6px 10px">${c.avg_latency_ms || 0} ms</td><td style="text-align:right;padding:6px 10px">${waitStr}</td><td style="text-align:right;padding:6px 10px">$${(c.total_usd || 0).toFixed(4)}</td></tr>`;
      }).join('') || '<tr><td colspan="6" style="padding:8px;color:var(--text-muted);text-align:center">暂无成本数据</td></tr>';
      const conc = r.vlm_concurrency || {};
      const concBadge = (conc.total_calls > 0)
        ? `<div style="font-size:10px;color:var(--text-dim);margin-top:6px">VLM 并发 · 累计调用 <b>${conc.total_calls}</b> · 峰值等待 <b style="color:${conc.peak_wait_ms>2000?'#ef4444':'#a78bfa'}">${conc.peak_wait_ms}ms</b> · 平均等待 <b>${conc.total_calls?Math.round(conc.total_wait_ms/conc.total_calls):0}ms</b></div>`
        : '';
      body.innerHTML = `
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin-bottom:14px">${kpi}</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
          <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:10px;padding:10px">
            <div style="font-size:12px;font-weight:600;color:var(--text-dim);margin-bottom:6px">分画像</div>
            <table style="width:100%;border-collapse:collapse;font-size:11px">
              <thead><tr style="color:var(--text-muted)"><th style="text-align:left;padding:4px 10px;font-weight:500">画像</th><th style="text-align:right;padding:4px 10px;font-weight:500">L1</th><th style="text-align:right;padding:4px 10px;font-weight:500">L2</th><th style="text-align:right;padding:4px 10px;font-weight:500">命中</th></tr></thead>
              <tbody>${byPersona}</tbody>
            </table>
          </div>
          <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:10px;padding:10px">
            <div style="font-size:12px;font-weight:600;color:var(--text-dim);margin-bottom:6px">分设备（Top 10）</div>
            <table style="width:100%;border-collapse:collapse;font-size:11px">
              <thead><tr style="color:var(--text-muted)"><th style="text-align:left;padding:4px 10px;font-weight:500">设备</th><th style="text-align:right;padding:4px 10px;font-weight:500">L1</th><th style="text-align:right;padding:4px 10px;font-weight:500">L2</th><th style="text-align:right;padding:4px 10px;font-weight:500">命中</th></tr></thead>
              <tbody>${byDev}</tbody>
            </table>
          </div>
        </div>
        <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:10px;padding:10px;margin-top:12px">
          <div style="font-size:12px;font-weight:600;color:var(--text-dim);margin-bottom:6px">AI 成本 & 延迟（本地 VLM = $0）</div>
          <table style="width:100%;border-collapse:collapse;font-size:11px">
            <thead><tr style="color:var(--text-muted)"><th style="text-align:left;padding:4px 10px;font-weight:500">模型</th><th style="text-align:left;padding:4px 10px;font-weight:500">场景</th><th style="text-align:right;padding:4px 10px;font-weight:500">调用数</th><th style="text-align:right;padding:4px 10px;font-weight:500">平均耗时</th><th style="text-align:right;padding:4px 10px;font-weight:500">平均/峰值排队</th><th style="text-align:right;padding:4px 10px;font-weight:500">累计</th></tr></thead>
            <tbody>${costRows}</tbody>
          </table>
          ${concBadge}
        </div>
      `;
    } catch (e) {
      const body = document.getElementById('fb-ins-body');
      if (body) body.innerHTML = `<div style="color:#ef4444">加载失败: ${e.message || e}</div>`;
    }
  };

  // ════════════════════════════════════════════════════════
  // 目标客群（persona）列表
  // P2-UI Sprint：
  //   原先这里是 FB_QUICK_GEO —— 一个散装 GEO 列表 + 另一个 persona_key
  //   输入框，容易出现「IT + jp_female_midlife」这类错配。
  //
  //   现在改为从 /facebook/active-persona 读后端 YAML 定义好的客群，
  //   一次锁定「国家 + 年龄 + 性别 + 兴趣 + 引流优先级」，
  //   前端不再可能凭空组合。添加/修改客群 → 改 fb_target_personas.yaml
  //   即可热加载生效，不必改前端。
  // ════════════════════════════════════════════════════════
  // 本地仅保留一个最小回退项，真正列表来自 _fbAvailablePersonas。
  const _FB_PERSONA_FALLBACK = [
    { persona_key: '', display_flag: '🌐', display_label: '🌐 全球(未绑定客群)',
      short_label: '全球', country_code: '', language: 'en' },
  ];

  function _fbPersonaOptions() {
    const arr = (_fbAvailablePersonas && _fbAvailablePersonas.length)
      ? _fbAvailablePersonas
      : _FB_PERSONA_FALLBACK;
    const activeKey = (_fbActivePersona && _fbActivePersona.persona_key) || '';
    return arr.map(function (p) {
      const sel = (p.persona_key === activeKey) ? 'selected' : '';
      return `<option value="${p.persona_key}" ${sel}>${p.display_label}</option>`;
    }).join('');
  }

  // ════════════════════════════════════════════════════════
  // 5 套执行方案模态(全设备 / 选定设备)
  // ════════════════════════════════════════════════════════
  window.fbOpenPresetsModal = async function (preselectedDevice) {
    await _fbLoadActivePersona();
    await _fbLoadPresets();
    const presets = _fbPresets || [];

    const overlay = _fbModalOverlay('fb-presets-modal');
    const presetCards = presets.map(function (p) {
      const stepsTxt = (p.steps || []).map(function (s) { return s.type.replace('facebook_', ''); }).join(' → ');
      return `
        <div onclick="fbLaunchPresetWithPersona('${p.key}', ${preselectedDevice ? `'${preselectedDevice}'` : 'null'})"
             style="background:var(--bg-main);border:1px solid var(--border);border-left:4px solid ${p.color};border-radius:10px;padding:14px;cursor:pointer;transition:transform .15s"
             onmouseover="this.style.transform='translateY(-2px)';this.style.borderColor='${p.color}'"
             onmouseout="this.style.transform='';this.style.borderColor='${p.color}'">
          <div style="font-size:15px;font-weight:700;margin-bottom:4px">${p.name}</div>
          <div style="font-size:11px;color:var(--text-muted);margin-bottom:8px">${p.desc}</div>
          <div style="font-size:10px;color:var(--text-dim);margin-bottom:8px">${p.detail}</div>
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:10px">
            <span style="color:var(--text-dim)">≈ ${p.estimated_minutes} 分钟</span>
            <span style="color:${p.color};font-weight:600">${p.estimated_output}</span>
          </div>
          <div style="margin-top:8px;font-size:9px;color:var(--text-dim);font-family:monospace">${stepsTxt}</div>
          <button style="margin-top:10px;width:100%;padding:6px;background:${p.color};color:#fff;border:none;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer">
            ▶ 一键启动
          </button>
        </div>
      `;
    }).join('');

    // P2-UI Sprint：用 persona 下拉替代原先的 GEO 选择器
    const personaOptions = _fbPersonaOptions();
    const persona = _fbActivePersona || {};
    // 默认群组占位符：从 persona.seed_group_keywords 取前 2 条
    const seedHint = (persona.seed_group_keywords || []).slice(0, 2).join(', ');
    const personaTopics = (persona.interest_topics || []).slice(0, 5).join(' · ');

    overlay.innerHTML = `
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:20px;max-width:960px;width:96%;max-height:88vh;overflow-y:auto">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
          <div>
            <div style="font-size:18px;font-weight:700">⚡ Facebook 执行方案</div>
            <div style="font-size:12px;color:var(--text-muted);margin-top:2px">
              ${preselectedDevice ? '设备 ' + preselectedDevice.substring(0, 8) : '将下发到所有在线 FB 设备'}
            </div>
          </div>
          <button onclick="document.getElementById('fb-presets-modal').remove()" style="background:none;border:none;color:var(--text-muted);font-size:22px;cursor:pointer">✕</button>
        </div>

        <!-- 目标客群选择器（替代原 GEO + persona_key 两个散装字段） -->
        <div style="display:flex;gap:12px;align-items:center;margin-bottom:8px;padding:10px 12px;background:var(--bg-main);border:1px solid var(--border);border-radius:8px;flex-wrap:wrap">
          <span style="font-size:12px;color:var(--text-muted)">🎯 目标客群:</span>
          <select id="fb-persona-select" onchange="fbOnPersonaChange()"
            style="background:var(--bg-card);border:1px solid var(--border);color:var(--text);padding:5px 10px;border-radius:6px;font-size:12px;min-width:220px">
            ${personaOptions}
          </select>
          <span style="font-size:11px;color:var(--text-dim)" id="fb-persona-topics">
            ${personaTopics ? '兴趣: ' + personaTopics : ''}
          </span>
        </div>

        <!-- 目标群组(可选覆盖 persona 默认种子群) -->
        <div style="display:flex;gap:12px;align-items:center;margin-bottom:12px;padding:10px 12px;background:var(--bg-main);border:1px solid var(--border);border-radius:8px;flex-wrap:wrap">
          <span style="font-size:12px;color:var(--text-muted)">👥 目标群组(可选):</span>
          <input type="text" id="fb-target-groups"
            placeholder="${seedHint ? '留空用客群默认 → ' + seedHint : '如: ママ友, アラフィフ 趣味'}"
            style="flex:1;min-width:180px;background:var(--bg-card);border:1px solid var(--border);color:var(--text);padding:5px 10px;border-radius:6px;font-size:12px">
          <span style="font-size:10px;color:var(--text-dim)">留空时自动使用客群默认种子群</span>
        </div>

        <div style="background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.3);border-radius:8px;padding:8px 12px;font-size:11px;color:#fbbf24;margin-bottom:14px">
          ⚠ 启动前确认：账号已登录、Messenger 已就绪、VPN 已连接到 <b id="fb-geo-hint">${persona.country_code || '目标'}</b> 地区
        </div>

        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px">
          ${presetCards || '<div style="color:#f87171">未加载到预设(请检查 /facebook/presets)</div>'}
        </div>

        <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border);font-size:11px;color:var(--text-dim)">
          💡 客群参数来自 <code>config/fb_target_personas.yaml</code> 热加载；风控节奏来自
          <code>facebook_playbook.yaml</code> 按账号阶段(cold_start/growth/mature/cooldown)自动匹配。<br>
          切换客群 = 同时切换国家/语言/引流渠道优先级/默认群组，一次锁定不会错配。
        </div>
      </div>
    `;
  };

  // persona 切换时更新兴趣提示 + GEO 提示
  window.fbOnPersonaChange = function () {
    const sel = document.getElementById('fb-persona-select');
    if (!sel) return;
    const key = sel.value;
    const p = (_fbAvailablePersonas || []).find(function (x) { return x.persona_key === key; })
      || _fbActivePersona || {};
    const topicsEl = document.getElementById('fb-persona-topics');
    if (topicsEl) {
      const topics = (p.interest_topics || []).slice(0, 5).join(' · ');
      topicsEl.textContent = topics ? ('兴趣: ' + topics) : '';
    }
    const geoHint = document.getElementById('fb-geo-hint');
    if (geoHint) geoHint.textContent = p.country_code || '目标';
    // 更新目标群组占位
    const gInput = document.getElementById('fb-target-groups');
    if (gInput && !gInput.value) {
      const seed = (p.seed_group_keywords || []).slice(0, 2).join(', ');
      gInput.placeholder = seed ? ('留空用客群默认 → ' + seed) : '如: 自定义群名1, 群名2';
    }
  };

  // 包装版:从模态读取 persona + 群组,再调用 fbLaunchPreset
  window.fbLaunchPresetWithPersona = function (presetKey, deviceId) {
    const personaSel = document.getElementById('fb-persona-select');
    const groupsInput = document.getElementById('fb-target-groups');
    const persona_key = (personaSel && personaSel.value) || '';
    const p = (_fbAvailablePersonas || []).find(function (x) { return x.persona_key === persona_key; })
      || _fbActivePersona || {};
    const target_country = p.country_code || '';
    const language = p.language || '';
    const target_groups = (groupsInput && groupsInput.value || '')
      .split(',').map(function (g) { return g.trim(); }).filter(Boolean);

    // 2026-04-23: 检查预设的 needs_input（如 name_hunter 需要 add_friend_targets）
    const preset = (_fbPresets || []).find(function (x) { return x.key === presetKey; });
    const needs = (preset && preset.needs_input) || [];
    if (needs.includes('add_friend_targets')) {
      // 弹输入框收集名字列表
      fbOpenNameHunterInput(presetKey, deviceId, {
        persona_key: persona_key,
        target_country: target_country,
        language: language,
        target_groups: target_groups,
      });
      return;
    }

    fbLaunchPreset(presetKey, deviceId, {
      persona_key: persona_key,
      target_country: target_country,
      language: language,
      target_groups: target_groups,
    });
  };

  // 2026-04-23: 点名添加输入模态 —— 收集名字列表 + 可选打招呼文案覆盖
  window.fbOpenNameHunterInput = function (presetKey, deviceId, extra) {
    const overlay = _fbModalOverlay('fb-name-hunter-input');
    const personaLabel = (extra && extra.persona_key) || '默认';
    // 尝试回读上次输入,方便运营复用
    let lastNames = '';
    let lastGreeting = '';
    try {
      lastNames = localStorage.getItem('fb_name_hunter_last') || '';
      lastGreeting = localStorage.getItem('fb_name_hunter_greeting') || '';
    } catch (e) { /* ignore */ }
    overlay.innerHTML = `
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:20px;max-width:580px;width:96%">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
          <div>
            <div style="font-size:17px;font-weight:700">🔎 点名添加 — 输入目标名字</div>
            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
              客群: <code>${personaLabel}</code> · 每行一个名字,或逗号分隔
            </div>
          </div>
          <button onclick="document.getElementById('fb-name-hunter-input').remove()"
                  style="background:none;border:none;color:var(--text-muted);font-size:22px;cursor:pointer">✕</button>
        </div>
        <textarea id="fb-nh-names" rows="8"
          placeholder="山田花子&#10;佐藤美咲&#10;鈴木 由美"
          style="width:100%;box-sizing:border-box;background:var(--bg-main);border:1px solid var(--border);color:var(--text);padding:10px;border-radius:8px;font-size:13px;font-family:inherit;resize:vertical">${lastNames.replace(/</g,'&lt;')}</textarea>
        ${lastNames ? '<div style="font-size:10px;color:var(--text-dim);margin-top:4px">🕑 已回填上次输入</div>' : ''}
        <div style="margin-top:10px">
          <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">打招呼文案(可选,为空则按客群自动生成本地化问候)</div>
          <textarea id="fb-nh-greeting" rows="2"
            placeholder="例:はじめまして😊つながれて嬉しいです🌸"
            style="width:100%;box-sizing:border-box;background:var(--bg-main);border:1px solid var(--border);color:var(--text);padding:8px;border-radius:6px;font-size:12px;font-family:inherit;resize:vertical">${lastGreeting.replace(/</g,'&lt;')}</textarea>
        </div>
        <div style="margin-top:10px;padding:8px 12px;background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.3);border-radius:6px;font-size:11px;color:#fbbf24">
          ⚠ 单次任务最多处理 playbook.max_friends_per_run 个(mature=5 / growth=3 / cold_start=0);
          phase=cold_start/cooldown 会整体跳过打招呼。
        </div>
        <div style="margin-top:14px;display:flex;gap:8px;justify-content:flex-end">
          <button onclick="document.getElementById('fb-name-hunter-input').remove()"
                  style="padding:8px 16px;background:none;border:1px solid var(--border);color:var(--text-muted);border-radius:8px;cursor:pointer">取消</button>
          <button id="fb-nh-submit"
                  style="padding:8px 16px;background:#0ea5e9;color:#fff;border:none;border-radius:8px;font-weight:600;cursor:pointer">
            ▶ 启动点名添加
          </button>
        </div>
      </div>
    `;
    const submitBtn = document.getElementById('fb-nh-submit');
    submitBtn.onclick = function () {
      const namesRaw = (document.getElementById('fb-nh-names') || {}).value || '';
      const greetingRaw = (document.getElementById('fb-nh-greeting') || {}).value || '';
      // 拆分为 [{"name": "..."}, ...]  后端也有兜底拆分,此处优先客户端标准化。
      const names = namesRaw.split(/[,\n;]+/)
        .map(function (s) { return s.trim(); })
        .filter(Boolean);
      if (!names.length) {
        showToast('请至少输入 1 个名字', 'warning');
        return;
      }
      // 2026-04-23: playbook.send_greeting.max_friends_per_run 当前最高 mature=5
      // 超过这个数的名字会被静默丢弃,提示用户一下,避免误会
      const softLimit = 20;
      if (names.length > softLimit) {
        if (!confirm('你输入了 ' + names.length + ' 个名字, playbook 每次任务通常只处理前 3~5 个, '
                     + '其余会在下次 run 继续(24h 上限仍按 daily_cap 管控)。继续?')) {
          return;
        }
      }
      const payload = Object.assign({}, extra || {}, {
        add_friend_targets: names.map(function (n) { return { name: n }; }),
      });
      // 记住本次输入,方便刷新后复用
      try {
        localStorage.setItem('fb_name_hunter_last', namesRaw);
        if (greetingRaw) localStorage.setItem('fb_name_hunter_greeting', greetingRaw);
      } catch (e) { /* localStorage may be disabled */ }
      if (greetingRaw.trim()) payload.greeting = greetingRaw.trim();
      // 关闭输入模态,再走常规 launch 路径
      const m = document.getElementById('fb-name-hunter-input');
      if (m) m.remove();
      fbLaunchPreset(presetKey, deviceId, payload);
    };
  };

  // 向后兼容旧名字（设备侧边栏、其他入口可能还在调）
  window.fbLaunchPresetWithGeo = window.fbLaunchPresetWithPersona;

  // 顶部指挥栏的"目标客群"徽章点击：打开快速切换
  window.fbOpenPersonaPicker = async function () {
    await _fbLoadActivePersona(true);
    const overlay = _fbModalOverlay('fb-persona-picker');
    const list = (_fbAvailablePersonas || []).map(function (p) {
      const active = (_fbActivePersona && p.persona_key === _fbActivePersona.persona_key);
      const pk = (p.persona_key || '').replace(/'/g, '');
      const setBtn = (!active && pk)
        ? `<button type="button" onclick="fbSetActivePersona('${pk}')"
            style="margin-top:8px;padding:6px 10px;font-size:11px;border-radius:6px;border:1px solid var(--border);background:var(--bg-card);cursor:pointer">
            设为全局默认</button>`
        : '';
      return `
        <div style="background:var(--bg-main);border:1px solid ${active?'#ec4899':'var(--border)'};
                    border-radius:10px;padding:12px;margin-bottom:8px">
          <div style="display:flex;align-items:center;gap:10px">
            <span style="font-size:22px">${p.display_flag || '🌐'}</span>
            <div style="flex:1">
              <div style="font-weight:600;font-size:14px">${p.display_label}</div>
              <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
                引流优先: ${(p.referral_priority||[]).map(function(c){return (_FB_CHANNEL_META[c]||{}).zh||c;}).join(' › ')}
              </div>
            </div>
            ${active ? '<span style="color:#ec4899;font-weight:600;font-size:12px">✓ 当前</span>' : ''}
          </div>
          ${(p.interest_topics||[]).length ? `
            <div style="font-size:10px;color:var(--text-dim);margin-top:8px;padding-top:8px;border-top:1px solid var(--border)">
              兴趣: ${p.interest_topics.slice(0,8).join(' · ')}
            </div>` : ''}
          ${setBtn}
        </div>`;
    }).join('');
    const ovHint = _fbPersonaOverrideKey
      ? `<div style="font-size:10px;color:#f59e0b;margin-bottom:8px">运行时覆盖: <code>${_fbPersonaOverrideKey}</code> · YAML 默认: <code>${_fbPersonaYamlDefault || '—'}</code></div>`
      : `<div style="font-size:10px;color:var(--text-dim);margin-bottom:8px">YAML 默认: <code>${_fbPersonaYamlDefault || '—'}</code>（未设运行时覆盖）</div>`;
    const clrBtn = _fbPersonaOverrideKey
      ? `<button type="button" onclick="fbClearPersonaOverride()"
          style="padding:8px 12px;font-size:12px;border-radius:8px;border:1px solid var(--border);background:transparent;cursor:pointer;margin-right:8px">
          清除覆盖（恢复 YAML default）</button>`
      : '';
    overlay.innerHTML = `
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:20px;max-width:560px;width:96%;max-height:86vh;overflow-y:auto">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
          <div>
            <div style="font-size:17px;font-weight:700">🎯 目标客群</div>
            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
              点「设为全局默认」写入本机 <code>data/fb_active_persona_override.json</code>，不改 YAML；改 YAML 的 <code>default_persona</code> 为永久切换。
            </div>
          </div>
          <button onclick="document.getElementById('fb-persona-picker').remove()"
            style="background:none;border:none;color:var(--text-muted);font-size:22px;cursor:pointer">✕</button>
        </div>
        ${ovHint}
        <div style="margin-bottom:10px">${clrBtn}</div>
        ${list || '<div style="color:#f87171">未加载到客群</div>'}
      </div>
    `;
  };

  window.fbSetActivePersona = async function (personaKey) {
    try {
      await api('POST', '/facebook/active-persona', { persona_key: personaKey });
      if (typeof showToast === 'function') showToast('已切换全局默认客群', 'success');
      _fbPresets = null;
      await _fbLoadPresets(true);
      await _fbLoadActivePersona(true);
      await _fbLoadReferrals();
      _fbRenderCommandBar();
      const m = document.getElementById('fb-persona-picker');
      if (m) m.remove();
    } catch (e) {
      if (typeof showToast === 'function') showToast(e.message || String(e), 'error');
    }
  };

  window.fbClearPersonaOverride = async function () {
    try {
      await api('POST', '/facebook/active-persona', { clear: true });
      if (typeof showToast === 'function') showToast('已清除运行时覆盖', 'success');
      _fbPresets = null;
      await _fbLoadPresets(true);
      await _fbLoadActivePersona(true);
      await _fbLoadReferrals();
      _fbRenderCommandBar();
      const m = document.getElementById('fb-persona-picker');
      if (m) m.remove();
    } catch (e) {
      if (typeof showToast === 'function') showToast(e.message || String(e), 'error');
    }
  };

  // ════════════════════════════════════════════════════════
  // Facebook launch 响应摘要（供预设启动与其它调用方复用）
  // ════════════════════════════════════════════════════════
  function _fbFormatLaunchStepErrors(tasks) {
    var bad = (tasks || []).filter(function (t) { return t && !t.ok; });
    if (!bad.length) return '';
    var parts = bad.map(function (t) {
      var msg = (t.error != null && String(t.error)) ? String(t.error) : '';
      if (t.detail && typeof t.detail === 'object') {
        var de = (t.detail.error || t.detail.message || '').trim();
        if (de) msg = de + (msg && msg !== de ? (' (' + msg + ')') : '');
      }
      return (t.type || '?') + (t.http_status ? ' [HTTP ' + t.http_status + ']' : '') + ': ' + (msg || '失败');
    });
    return parts.join(' · ').slice(0, 420);
  }

  /**
   * @param {object|null} data - POST /facebook/device/.../launch 的 JSON
   * @param {string} deviceId
   * @returns {{ fullDeviceOk: boolean, taskCount: number, stepCount: number, errorLine: string }}
   */
  window.fbSummarizeLaunchResponse = function (data, deviceId) {
    var tasks = (data && data.flow_tasks) || [];
    var nOk = (data && data.task_count) || 0;
    var pref = ((deviceId || '').substring(0, 8) || '?') + '… ';
    var errBlob = _fbFormatLaunchStepErrors(tasks);
    return {
      fullDeviceOk: nOk === tasks.length && tasks.length > 0,
      taskCount: nOk,
      stepCount: tasks.length,
      errorLine: errBlob ? (pref + errBlob) : ''
    };
  };

  // ════════════════════════════════════════════════════════
  // 启动单个预设
  // ════════════════════════════════════════════════════════
  window.fbLaunchPreset = async function (presetKey, deviceId, extra) {
    extra = extra || {};
    let devices = [];
    if (deviceId) {
      devices = [deviceId];
    } else {
      // 全设备模式 — 从 platform-grid 缓存或重新查
      try {
        const r = await api('GET', '/platforms/facebook/device-grid');
        devices = ((r && r.devices) || []).filter(function (d) { return d.online; }).map(function (d) { return d.device_id; });
      } catch (e) {
        showToast('无法获取设备列表: ' + e.message, 'error');
        return;
      }
    }

    if (!devices.length) {
      showToast('没有在线设备可启动', 'warning');
      return;
    }

    const geoTxt = extra.target_country ? ('  GEO=' + extra.target_country) : '';
    const grpTxt = (extra.target_groups || []).length ? ('  目标群=' + extra.target_groups.length + '个') : '';
    const nameTxt = (extra.add_friend_targets || []).length
      ? ('  名字=' + extra.add_friend_targets.length + '个') : '';
    if (!confirm('将在 ' + devices.length + ' 台设备上启动「' + presetKey + '」预设'
                 + geoTxt + grpTxt + nameTxt + ',确认?')) return;

    let okCount = 0;
    let workerCapWarnShown = false;
    for (const did of devices) {
      try {
        const body = { preset_key: presetKey };
        if (extra.persona_key) body.persona_key = extra.persona_key;
        if (extra.target_country) body.target_country = extra.target_country;
        if (extra.language) body.language = extra.language;
        if (extra.target_groups && extra.target_groups.length) body.target_groups = extra.target_groups;
        // 2026-04-23: 点名添加 / 独立打招呼 新增字段
        if (extra.add_friend_targets && extra.add_friend_targets.length) {
          body.add_friend_targets = extra.add_friend_targets;
        }
        if (extra.greeting) body.greeting = extra.greeting;
        if (extra.verification_note) body.verification_note = extra.verification_note;
        const data = await api('POST', '/facebook/device/' + did + '/launch', body);
        if (data && data.worker_capabilities_warning && typeof showToast === 'function' && !workerCapWarnShown) {
          workerCapWarnShown = true;
          showToast(String(data.worker_capabilities_warning).slice(0, 480), 'warning');
        }
        const sum = window.fbSummarizeLaunchResponse(data, did);
        if (sum.errorLine && typeof showToast === 'function') {
          showToast(sum.errorLine, 'error');
        }
        if (sum.fullDeviceOk) okCount += 1;
        else if (sum.taskCount > 0 && sum.stepCount && typeof showToast === 'function') {
          showToast((did || '').substring(0, 8) + '… 仅 ' + sum.taskCount + '/' + sum.stepCount + ' 步入队', 'warning');
        }
      } catch (e) {
        console.warn('launch failed for ' + did, e);
        if (typeof showToast === 'function') showToast((did || '').substring(0, 8) + '… ' + (e.message || e), 'error');
      }
    }

    showToast('已下发到 ' + okCount + '/' + devices.length + ' 台设备', okCount === devices.length ? 'success' : 'warning');
    const m = document.getElementById('fb-presets-modal');
    if (m) m.remove();
  };

  // ════════════════════════════════════════════════════════
  // 引流账号配置(默认 WA 优先排序)
  // ════════════════════════════════════════════════════════
  window.fbOpenReferralModal = async function () {
    await _fbLoadActivePersona();
    await _fbLoadReferrals();
    const refs = _fbReferrals || {};
    // priority_order 从 /referral-config 返回（按当前 persona 排序）
    const order = (_fbReferralPriority && _fbReferralPriority.length)
      ? _fbReferralPriority
      : ['whatsapp', 'telegram', 'instagram', 'line'];
    const persona = _fbActivePersona || {};

    const overlay = _fbModalOverlay('fb-referral-modal');

    // ── 表头：按 priority_order 动态生成列（主引流渠道放第一列并高亮）
    const tableHeaders = order.map(function (ch, i) {
      const meta = _FB_CHANNEL_META[ch] || { icon: '·', zh: ch };
      const main = (i === 0);
      return `<th style="text-align:left;padding:8px;font-weight:600;${main?'color:#ec4899':'color:var(--text-muted)'}">
        ${meta.icon} ${meta.zh}${main?' <span style="font-size:9px;padding:1px 4px;background:rgba(236,72,153,.2);border-radius:3px">主</span>':''}
      </th>`;
    }).join('');

    const tableRows = Object.keys(refs).length
      ? Object.entries(refs).map(function (entry) {
          const did = entry[0];
          const r = entry[1] || {};
          const cells = order.map(function (ch) {
            return `<td style="padding:6px 8px;font-size:12px">${r[ch] || '<span style="color:var(--text-dim)">—</span>'}</td>`;
          }).join('');
          return `<tr>
            <td style="padding:6px 8px;font-family:monospace;font-size:11px">${did.substring(0, 12)}…</td>
            ${cells}
          </tr>`;
        }).join('')
      : `<tr><td colspan="${order.length + 1}" style="text-align:center;padding:14px;color:var(--text-muted);font-size:12px">尚未配置任何引流账号</td></tr>`;

    // ── 批量配置输入框：按 priority_order 渲染，主渠道 placeholder 带客群提示
    const inputBlocks = order.map(function (ch, i) {
      const meta = _FB_CHANNEL_META[ch] || { icon: '·', zh: ch, placeholder: '', note: '' };
      const main = (i === 0);
      const hint = main ? `(主${persona.country_zh ? ' · ' + persona.country_zh + '客群' : ''})` : `(备选·${meta.note || ''})`;
      return `
        <div>
          <label style="font-size:11px;color:var(--text-muted)">${meta.icon} ${meta.zh} ${hint}</label>
          <input type="text" id="fb-ref-${ch}" placeholder="${meta.placeholder}"
            style="width:100%;padding:6px;background:var(--bg-main);border:1px solid ${main?'rgba(236,72,153,.4)':'var(--border)'};border-radius:6px;color:var(--text);font-size:12px;margin-top:2px">
        </div>`;
    }).join('');

    // ── 客群感知的提示语
    const mainCh = order[0] || 'whatsapp';
    const mainMeta = _FB_CHANNEL_META[mainCh] || { zh: mainCh };
    const personaHint = persona.short_label
      ? `当前目标客群 <b>${persona.display_flag || ''} ${persona.short_label}</b>：${mainMeta.zh} 渗透率最高，主引流优先填 ${mainMeta.zh}。`
      : `未配置目标客群 → 使用全球默认优先级（WA > TG > IG > LINE）。`;

    overlay.innerHTML = `
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:20px;max-width:760px;width:96%;max-height:88vh;overflow-y:auto">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
          <div style="font-size:18px;font-weight:700">🔗 Facebook 引流账号</div>
          <button onclick="document.getElementById('fb-referral-modal').remove()" style="background:none;border:none;color:var(--text-muted);font-size:22px;cursor:pointer">✕</button>
        </div>

        <div style="background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.3);border-radius:8px;padding:8px 12px;font-size:11px;color:#4ade80;margin-bottom:14px">
          💡 ${personaHint}
        </div>

        <div style="margin-bottom:14px">
          <div style="font-size:13px;font-weight:600;margin-bottom:8px">批量配置 (适用全部 FB 设备)</div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px">
            ${inputBlocks}
          </div>
          <button onclick="fbSaveReferralBatch()" style="margin-top:10px;background:linear-gradient(135deg,#22c55e,#16a34a);color:#fff;border:none;padding:6px 14px;font-size:12px;font-weight:600;border-radius:6px;cursor:pointer">💾 应用到全部设备</button>
          <span style="margin-left:10px;font-size:10px;color:var(--text-dim)">只填有值的框；空框不会清除已有配置</span>
        </div>

        <div style="font-size:13px;font-weight:600;margin-bottom:6px">当前配置</div>
        <div style="overflow-x:auto">
        <table style="width:100%;border-collapse:collapse;font-size:11px;background:var(--bg-main);border-radius:6px;overflow:hidden;min-width:520px">
          <thead>
            <tr style="background:rgba(255,255,255,.03)">
              <th style="text-align:left;padding:8px;font-weight:600">设备</th>
              ${tableHeaders}
            </tr>
          </thead>
          <tbody>${tableRows}</tbody>
        </table>
        </div>
      </div>
    `;
  };

  window.fbSaveReferralBatch = async function () {
    // 遍历 priority_order 的所有渠道输入框，非空就加进 body
    const order = (_fbReferralPriority && _fbReferralPriority.length)
      ? _fbReferralPriority
      : ['whatsapp', 'telegram', 'instagram', 'line'];
    const body = { all: true };
    let anyValue = false;
    order.forEach(function (ch) {
      const el = document.getElementById('fb-ref-' + ch);
      if (!el) return;
      const v = (el.value || '').trim();
      if (v) {
        body[ch] = v;
        anyValue = true;
      }
    });
    if (!anyValue) {
      showToast('请至少填写一个账号', 'warning');
      return;
    }
    try {
      const r = await api('POST', '/facebook/referral-config', body);
      showToast('已应用到 ' + (r.updated || 0) + ' 台设备', 'success');
      _fbReferrals = null;
      const m = document.getElementById('fb-referral-modal');
      if (m) m.remove();
      setTimeout(function () { fbOpenReferralModal(); }, 200);
    } catch (e) {
      showToast('保存失败: ' + e.message, 'error');
    }
  };

  // ════════════════════════════════════════════════════════
  // 「批量发请求」入口 —— P2-UI Sprint 已在顶部栏删除该按钮,
  // 但保留别名供旧代码 / 设备卡片侧栏 / 高分线索模态回调 (fbBatchRequestSingle)。
  // 底层就是 fbOpenPresetsModal，避免多路径维护。
  // ════════════════════════════════════════════════════════
  window.fbBatchRequest = function () {
    fbOpenPresetsModal(null);
  };

  // ════════════════════════════════════════════════════════
  // Modal 工具
  // ════════════════════════════════════════════════════════
  function _fbModalOverlay(id) {
    let overlay = document.getElementById(id);
    if (overlay) overlay.remove();
    overlay = document.createElement('div');
    overlay.id = id;
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px';
    overlay.onclick = function (ev) { if (ev.target === overlay) overlay.remove(); };
    document.body.appendChild(overlay);
    return overlay;
  }

  // 暴露给设备卡片侧栏使用 — 用户在卡片上点"配置流程",带上设备 ID 进入预设模态
  window.fbDeviceConfigFlow = function (deviceId) {
    fbOpenPresetsModal(deviceId);
  };

  // ════════════════════════════════════════════════════════
  // Sprint 2 新增 — 用 PlatShell 公共组件渲染
  // ════════════════════════════════════════════════════════

  window.fbOpenFunnelModal = async function () {
    const Shell = window.PlatShell;
    if (!Shell) { showToast('PlatShell 未加载', 'error'); return; }
    Shell.modal.open('fb-funnel-modal',
      '<div id="fb-funnel-body">加载中…</div>', { maxWidth: '860px' });
    try {
      const r = await Shell.api.get('/facebook/funnel?since_hours=168');
      const steps = r.steps || [];
      // P3-4: greeting 维度专属数据(取自 /facebook/funnel 响应根字段)
      const greetSent = r.stage_greetings_sent || 0;
      const greetFallback = r.stage_greetings_fallback || 0;
      const frSent = r.stage_friend_request_sent || 0;
      const rateGreetAfterAdd = r.rate_greet_after_add || 0;
      const templateDist = r.greeting_template_distribution || [];

      // 模板分布 top 5 的水平柱状
      const maxCnt = Math.max(1, ...templateDist.map(function (kv) { return kv[1] || 0; }));
      const tplBars = templateDist.length
        ? templateDist.map(function (kv) {
            const tid = kv[0] || '-';
            const cnt = kv[1] || 0;
            const w = Math.round(cnt * 100 / maxCnt);
            return ''
              + '<div style="display:flex;align-items:center;gap:8px;font-size:11px;margin-bottom:4px">'
              +   '<code style="min-width:110px;color:var(--text-muted)">' + tid + '</code>'
              +   '<div style="flex:1;background:rgba(96,165,250,.12);border-radius:4px;overflow:hidden;height:14px">'
              +     '<div style="width:' + w + '%;height:100%;background:linear-gradient(90deg,#60a5fa,#22d3ee)"></div>'
              +   '</div>'
              +   '<span style="min-width:32px;text-align:right;font-weight:600">' + cnt + '</span>'
              + '</div>';
          }).join('')
        : '<div style="color:var(--text-dim);font-size:11px">暂无 greeting 模板命中数据</div>';

      const fallbackPct = greetSent > 0 ? Math.round(greetFallback * 100 / greetSent) : 0;
      const fbPctColor = fallbackPct > 30 ? '#ef4444'
                       : fallbackPct > 10 ? '#f59e0b' : '#22c55e';

      const html = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '<h3 style="margin:0">📊 Facebook 引流漏斗 (近 7 天)</h3>'
        + '<button onclick="PlatShell.modal.close(\'fb-funnel-modal\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '</div>'
        + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">'
        //  ── 左列: 传统漏斗 (已有)
        + '<div>'
        +   '<div style="font-size:12px;color:var(--text-muted);margin-bottom:8px">🔻 主链路</div>'
        +   '<div style="display:grid;gap:8px">'
        +     steps.map(function (s) {
            const rate = s.rate != null ? ' (' + (s.rate * 100).toFixed(0) + '%)' : '';
            return '<div style="display:flex;justify-content:space-between;padding:8px 12px;background:var(--bg-main);border-radius:6px;border-left:3px solid #1877f2">'
              + '<span style="font-size:12px">' + s.label + '</span>'
              + '<span style="font-weight:700;color:#60a5fa;font-size:13px">' + s.value + rate + '</span>'
              + '</div>';
          }).join('')
        +   '</div>'
        + '</div>'
        //  ── 右列: greeting 专项 (新增)
        + '<div>'
        +   '<div style="font-size:12px;color:var(--text-muted);margin-bottom:8px">💬 打招呼 (P3)</div>'
        +   '<div style="display:grid;gap:8px;margin-bottom:12px">'
        +     '<div style="display:flex;justify-content:space-between;padding:8px 12px;background:var(--bg-main);border-radius:6px;border-left:3px solid #0ea5e9">'
        +       '<span style="font-size:12px">Greeting 总数</span>'
        +       '<span style="font-weight:700;color:#0ea5e9;font-size:13px">' + greetSent + '</span>'
        +     '</div>'
        +     '<div style="display:flex;justify-content:space-between;padding:8px 12px;background:var(--bg-main);border-radius:6px;border-left:3px solid ' + fbPctColor + '">'
        +       '<span style="font-size:12px">Fallback 路径</span>'
        +       '<span style="font-weight:700;color:' + fbPctColor + ';font-size:13px">' + greetFallback + ' (' + fallbackPct + '%)</span>'
        +     '</div>'
        +     '<div style="display:flex;justify-content:space-between;padding:8px 12px;background:var(--bg-main);border-radius:6px;border-left:3px solid #a855f7">'
        +       '<span style="font-size:12px">加友后打招呼率</span>'
        +       '<span style="font-weight:700;color:#a855f7;font-size:13px">' + (rateGreetAfterAdd * 100).toFixed(1) + '% (' + greetSent + '/' + frSent + ')</span>'
        +     '</div>'
        +   '</div>'
        +   '<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">模板命中 Top 5 (A/B 样本)</div>'
        +   '<div style="background:var(--bg-main);border-radius:6px;padding:10px">'
        +     tplBars
        +   '</div>'
        +   '<div style="margin-top:6px;font-size:10px;color:var(--text-dim)">'
        +     '回复率 A/B 需机器 B 的 Messenger 自动回复就位后查看 <code>/facebook/greeting-reply-rate</code>'
        +   '</div>'
        + '</div>'
        + '</div>'
        + '<div style="margin-top:14px;font-size:11px;color:var(--text-muted)">'
        + '设备范围: ' + (r._scope_device || 'all') + ' | 起始: ' + (r._scope_since || '?')
        + '</div>';
      document.getElementById('fb-funnel-body').innerHTML = html;
    } catch (e) {
      document.getElementById('fb-funnel-body').innerHTML =
        '<div style="color:#ef4444">加载失败: ' + e.message + '</div>';
    }
  };

  window.fbOpenRiskModal = async function () {
    const Shell = window.PlatShell;
    if (!Shell) { showToast('PlatShell 未加载', 'error'); return; }
    Shell.modal.open('fb-risk-modal',
      '<div id="fb-risk-body">加载中…</div>', { maxWidth: '760px' });
    try {
      const r = await Shell.api.get('/facebook/risk/status');
      const devs = r.devices || [];
      const cfg = r.config || {};
      const tableRows = devs.map(function (d) {
        const last = d.last_event || {};
        return '<tr>'
          + '<td style="padding:8px;font-family:monospace;font-size:11px">' + (d.device_id || '').substr(0, 12) + '…</td>'
          + '<td style="padding:8px">' + d.risk_count + '</td>'
          + '<td style="padding:8px;color:' + (d.cooldown_remaining > 0 ? '#ef4444' : '#22c55e') + '">'
          + (d.cooldown_remaining > 0 ? d.cooldown_remaining + 's' : '✓ 正常') + '</td>'
          + '<td style="padding:8px;font-size:11px;color:var(--text-muted)">'
          + (last.message || '—').substr(0, 40) + '</td>'
          + '<td style="padding:8px"><button onclick="fbClearRisk(\'' + d.device_id + '\')" style="background:rgba(34,197,94,.15);color:#22c55e;border:1px solid rgba(34,197,94,.4);padding:3px 8px;border-radius:4px;cursor:pointer;font-size:11px">清除</button></td>'
          + '</tr>';
      }).join('');
      document.getElementById('fb-risk-body').innerHTML = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '<h3 style="margin:0">🛡️ Facebook 风控自愈状态</h3>'
        + '<button onclick="PlatShell.modal.close(\'fb-risk-modal\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '</div>'
        + '<div style="background:var(--bg-main);border-radius:8px;padding:10px 14px;margin-bottom:14px;font-size:12px">'
        + '策略: <b>' + (cfg.strategy || '?') + '</b>(B=降级 warmup) | cooldown: ' + (cfg.cooldown_seconds || 0) + 's | 启用: ' + (cfg.enabled !== false ? '✓' : '✗')
        + '</div>'
        + '<table style="width:100%;border-collapse:collapse;font-size:12px">'
        + '<thead><tr style="background:rgba(255,255,255,.03)">'
        + '<th style="text-align:left;padding:8px">设备</th>'
        + '<th style="text-align:left;padding:8px">风控次数</th>'
        + '<th style="text-align:left;padding:8px">Cooldown</th>'
        + '<th style="text-align:left;padding:8px">最近消息</th>'
        + '<th style="text-align:left;padding:8px">操作</th>'
        + '</tr></thead><tbody>'
        + (tableRows || '<tr><td colspan="5" style="padding:14px;text-align:center;color:var(--text-muted)">暂无风控记录</td></tr>')
        + '</tbody></table>';
    } catch (e) {
      document.getElementById('fb-risk-body').innerHTML =
        '<div style="color:#ef4444">加载失败: ' + e.message + '</div>';
    }
  };

  window.fbClearRisk = async function (deviceId) {
    try {
      await window.PlatShell.api.post('/facebook/risk/clear/' + deviceId, {});
      showToast('已清除 ' + deviceId.substr(0, 8) + '… 的风控状态', 'success');
      fbOpenRiskModal();
    } catch (e) {
      showToast('清除失败: ' + e.message, 'error');
    }
  };

  window.fbOpenDailyBriefModal = async function () {
    const Shell = window.PlatShell;
    if (!Shell) { showToast('PlatShell 未加载', 'error'); return; }
    Shell.modal.open('fb-brief-modal',
      '<div id="fb-brief-body">加载中…</div>', { maxWidth: '780px' });
    const renderBody = function (md, meta) {
      // 简易 markdown → HTML(只支持 # ## - 列表、加粗、emoji)
      const html = md
        .replace(/^### (.*)$/gm, '<h4>$1</h4>')
        .replace(/^## (.*)$/gm, '<h3 style="margin-top:14px">$1</h3>')
        .replace(/^# (.*)$/gm, '<h2 style="margin-top:0">$1</h2>')
        .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
        .replace(/^- (.*)$/gm, '<li>$1</li>')
        .replace(/(<li>[\s\S]*?<\/li>)+/g, '<ul style="margin:6px 0;padding-left:20px">$&</ul>')
        .replace(/\n\n/g, '<br><br>');
      const m = meta || {};
      return ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '<h3 style="margin:0">📰 Facebook AI 日报</h3>'
        + '<div style="display:flex;gap:8px">'
        + '<button onclick="fbRegenerateBrief()" style="background:linear-gradient(135deg,#a855f7,#7c3aed);color:#fff;border:none;padding:5px 12px;border-radius:6px;font-size:11px;cursor:pointer">🔄 重新生成</button>'
        + '<button onclick="PlatShell.modal.close(\'fb-brief-modal\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '</div></div>'
        + '<div style="background:var(--bg-main);padding:14px;border-radius:8px;line-height:1.6;font-size:13px">'
        + html + '</div>'
        + '<div style="margin-top:10px;font-size:10px;color:var(--text-muted)">'
        + '生成时间: ' + (m.generated_at || '?') + ' | 窗口: ' + (m.window_hours || 24) + 'h | LLM: '
        + (m.llm_generated ? '✓' : '⚠ fallback 模板')
        + '</div>';
    };
    try {
      const r = await Shell.api.get('/facebook/daily-brief/latest?limit=1');
      const briefs = r.briefs || [];
      if (briefs.length === 0) {
        document.getElementById('fb-brief-body').innerHTML = ''
          + '<div style="text-align:center;padding:30px">'
          + '<div style="font-size:14px;margin-bottom:14px">尚无日报</div>'
          + '<button onclick="fbRegenerateBrief()" style="background:linear-gradient(135deg,#a855f7,#7c3aed);color:#fff;border:none;padding:8px 18px;border-radius:8px;cursor:pointer">🔄 立即生成第 1 份</button>'
          + '</div>';
        return;
      }
      const b = briefs[0];
      document.getElementById('fb-brief-body').innerHTML = renderBody(b.markdown || '', b);
    } catch (e) {
      document.getElementById('fb-brief-body').innerHTML =
        '<div style="color:#ef4444">加载失败: ' + e.message + '</div>';
    }
  };

  window.fbRegenerateBrief = async function () {
    const body = document.getElementById('fb-brief-body');
    if (body) body.innerHTML = '<div style="text-align:center;padding:20px">⏳ 正在调用 AI 生成…(可能需要 5~15 秒)</div>';
    try {
      await window.PlatShell.api.post('/facebook/daily-brief/generate?hours=24', {});
      showToast('日报已生成', 'success');
      fbOpenDailyBriefModal();
    } catch (e) {
      showToast('生成失败: ' + e.message, 'error');
    }
  };

  // ════════════════════════════════════════════════════════
  // Sprint 3 P1: 高分线索模态(用 PlatShell.leadList 公共组件)
  // ════════════════════════════════════════════════════════
  window.fbOpenLeadsModal = async function () {
    const html = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <div>
          <div style="font-size:18px;font-weight:700">🎯 Facebook 高分线索</div>
          <div style="font-size:11px;color:var(--text-dim);margin-top:2px">
            score≥60 的待加好友候选 · 点 "加好友" 立刻排队
          </div>
        </div>
        <div>
          <label style="font-size:11px;margin-right:8px">最低分:</label>
          <select id="fb-leads-minscore" onchange="fbReloadLeads()"
            style="padding:4px 8px;background:var(--bg-elev);color:var(--text);border:1px solid var(--border);border-radius:4px;font-size:11px">
            <option value="60">60(B+)</option>
            <option value="45">45(B)</option>
            <option value="80">80(S)</option>
          </select>
          <button onclick="fbReloadLeads()"
            style="margin-left:8px;padding:4px 10px;background:var(--accent);color:#fff;border:none;border-radius:4px;font-size:11px;cursor:pointer">
            🔄 刷新
          </button>
        </div>
      </div>
      <div id="fb-leads-body" style="font-size:12px">⏳ 加载中…</div>
    `;
    window.PlatShell.modal.open('fb-leads-modal', html, {
      title: '高分线索', maxWidth: '880px',
    });
    fbReloadLeads();
  };

  window.fbReloadLeads = async function () {
    const minScore = parseInt(document.getElementById('fb-leads-minscore').value) || 60;
    const body = document.getElementById('fb-leads-body');
    if (!body) return;
    body.innerHTML = '⏳ 加载中…';
    try {
      const data = await window.PlatShell.api.get(
        '/facebook/qualified-leads?limit=100&min_score=' + minScore);
      const leads = (data && data.leads) || [];
      body.innerHTML = window.PlatShell.leadList.render({
        leads: leads,
        actions: [
          { key: 'request', label: '加好友', color: '#1877f2' },
          { key: 'view',    label: '档案', color: '#6b7280' },
        ],
        onAction: function (action, lead) {
          if (action === 'request') {
            fbBatchRequestSingle(lead);
          } else if (action === 'view') {
            // 简单展示分数原因
            alert('Lead: ' + lead.name + '\n\n分数: ' + (lead.score || 0)
              + '\nTier: ' + (lead.tier || '?')
              + '\n\n原因:\n  · ' + (lead.reasons || lead.score_reasons || []).join('\n  · '));
          }
        },
      });
      // 头部加汇总
      const top = leads.length
        ? '<div style="margin-bottom:8px;font-size:11px;color:var(--text-dim)">'
          + '当前共 <b style="color:var(--text)">' + leads.length + '</b> 条 '
          + '· S 档 ' + leads.filter(function(l){return (l.tier||'')==='S';}).length
          + ' · A 档 ' + leads.filter(function(l){return (l.tier||'')==='A';}).length
          + ' · B 档 ' + leads.filter(function(l){return (l.tier||'')==='B';}).length
          + '</div>'
        : '';
      body.innerHTML = top + body.innerHTML;
    } catch (e) {
      body.innerHTML = '<div style="color:#ef4444">加载失败: ' + e.message + '</div>';
    }
  };

  window.fbBatchRequestSingle = function (lead) {
    if (!lead || !lead.name) return;
    if (!confirm('为 "' + lead.name + '" 创建 facebook_add_friend 任务?')) return;
    showToast('已加入队列(需要选定执行设备)', 'success');
    // 简化:跳到 BatchRequest 流程,带名字预填
    if (typeof fbBatchRequest === 'function') {
      fbBatchRequest();
    }
  };

})();
