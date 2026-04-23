/* platform-grid.js — 通用平台设备网格（复刻 TikTok 设备面板到所有平台）
   依赖: core.js (api, showToast, ALIAS, allDevices), platforms.js (PLAT_*, PLAT_PHASES)
*/

// ═══════════════════════════════════════════════════
// 平台专属配置：统计指标、操作按钮、卡片颜色
// ═══════════════════════════════════════════════════
const PLAT_GRID_CFG = {
  telegram: {
    stats: [
      {icon:'📨',key:'today_tasks',label:'今日任务'},
      {icon:'✅',key:'today_completed',label:'完成'},
      {icon:'❌',key:'today_failed',label:'失败'},
    ],
    quickActions: ['telegram_join_group','telegram_send_message','telegram_auto_reply'],
    accentColor: '#0088cc',
  },
  whatsapp: {
    stats: [
      {icon:'📨',key:'today_tasks',label:'今日任务'},
      {icon:'✅',key:'today_completed',label:'完成'},
      {icon:'❌',key:'today_failed',label:'失败'},
    ],
    quickActions: ['whatsapp_send_message','whatsapp_auto_reply','whatsapp_list_chats'],
    accentColor: '#25d366',
  },
  facebook: {
    stats: [
      {icon:'📨',key:'today_tasks',label:'今日任务'},
      {icon:'✅',key:'today_completed',label:'完成'},
      {icon:'❌',key:'today_failed',label:'失败'},
    ],
    quickActions: ['facebook_browse_feed','facebook_browse_feed_by_interest','facebook_add_friend','facebook_send_message'],
    accentColor: '#1877f2',
  },
  linkedin: {
    stats: [
      {icon:'📨',key:'today_tasks',label:'今日任务'},
      {icon:'✅',key:'today_completed',label:'完成'},
      {icon:'❌',key:'today_failed',label:'失败'},
    ],
    quickActions: ['linkedin_search_profile','linkedin_send_connection','linkedin_send_message'],
    accentColor: '#0a66c2',
  },
  instagram: {
    stats: [
      {icon:'📨',key:'today_tasks',label:'今日任务'},
      {icon:'✅',key:'today_completed',label:'完成'},
      {icon:'❌',key:'today_failed',label:'失败'},
    ],
    quickActions: ['instagram_browse_feed','instagram_search_leads','instagram_send_dm'],
    accentColor: '#e4405f',
  },
  twitter: {
    stats: [
      {icon:'📨',key:'today_tasks',label:'今日任务'},
      {icon:'✅',key:'today_completed',label:'完成'},
      {icon:'❌',key:'today_failed',label:'失败'},
    ],
    quickActions: ['twitter_browse_timeline','twitter_search_and_engage','twitter_send_dm'],
    accentColor: '#1d9bf0',
  },
};

// ═══════════════════════════════════════════════════
// 缓存 & 状态
// ═══════════════════════════════════════════════════
const _pgCache = {};   // platform -> {devices, summary, task_types}
let _pgLoading = {};
let _pgRefreshTimers = {};

// ═══════════════════════════════════════════════════
// 主入口：加载平台设备网格页面
// ═══════════════════════════════════════════════════
async function loadPlatGridPage(platform) {
  if (_pgLoading[platform]) return;
  _pgLoading[platform] = true;

  const container = document.querySelector(`#page-plat-${platform} .plat-page`);
  if (!container) { _pgLoading[platform] = false; return; }

  const color = PLAT_COLORS[platform] || '#60a5fa';
  const icon = PLAT_ICONS[platform] || '';
  const slogan = PLAT_SLOGANS[platform] || '';
  const cfg = PLAT_GRID_CFG[platform] || {};

  // 如果容器还没有网格结构，初始化骨架
  if (!container.querySelector('.pg-grid')) {
    container.innerHTML = _pgBuildSkeleton(platform, icon, slogan, color, cfg);
  }

  try {
    const [gridData] = await Promise.all([
      api('GET', `/platforms/${platform}/device-grid`),
      _fetchPlatTasks(platform),
    ]);
    _pgCache[platform] = gridData;

    // 更新汇总 pills
    const s = gridData.summary || {};
    _pgSetText(`pg-online-${platform}`, s.online || 0);
    _pgSetText(`pg-total-${platform}`, s.total || 0);
    _pgSetText(`pg-running-${platform}`, s.running_tasks || 0);

    // 渲染设备卡片
    _pgRenderGrid(platform, gridData.devices || []);

    // 渲染快捷操作（复用 platforms.js 的 PLAT_PHASES）
    _pgRenderQuickActions(platform, gridData.task_types || []);

    // 启动自动刷新
    _pgStartAutoRefresh(platform);

  } catch(e) {
    const grid = document.getElementById(`pg-device-grid-${platform}`);
    if (grid) grid.innerHTML = `<div style="grid-column:1/-1;text-align:center;color:#f87171;padding:24px;font-size:13px">${e.message||'加载失败'}</div>`;
  }
  _pgLoading[platform] = false;
}

// ═══════════════════════════════════════════════════
// 构建页面骨架 HTML
// ═══════════════════════════════════════════════════
function _pgBuildSkeleton(platform, icon, slogan, color, cfg) {
  const phases = PLAT_PHASES[platform] || [];
  return `
    <style>
      .pg-card{background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:10px 12px;cursor:pointer;transition:all .15s;position:relative;border-left:3px solid transparent}
      .pg-card:hover{border-color:${color}44;transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,0,0,.2)}
      .pg-card.state-online{border-left-color:#22c55e}
      .pg-card.state-busy{border-left-color:#3b82f6}
      .pg-card.state-idle{border-left-color:#94a3b8}
      .pg-card.state-offline{border-left-color:#ef4444;opacity:.65}
      .pg-card-head{display:flex;align-items:center;gap:6px;margin-bottom:6px}
      .pg-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
      .pg-alias{font-size:12px;font-weight:600;color:var(--text)}
      .pg-node{font-size:9px;padding:1px 5px;border-radius:3px;font-weight:600}
      .pg-stat-row{display:flex;gap:8px;font-size:10px;color:var(--text-muted);margin-bottom:4px}
      .pg-stat-row span b{color:var(--text);margin-left:2px}
      .pg-task-tag{font-size:9px;padding:2px 6px;border-radius:4px;font-weight:600}
      .pg-panel{position:fixed;top:0;right:-420px;width:400px;height:100vh;background:var(--bg-card);border-left:1px solid var(--border);z-index:500;transition:right .25s ease;overflow-y:auto;box-shadow:-4px 0 20px rgba(0,0,0,.3)}
      .pg-panel.open{right:0}
      .pg-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:499}
    </style>
    <!-- 顶部指挥栏 -->
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;flex-wrap:wrap;gap:8px">
      <div style="display:flex;align-items:center;gap:10px">
        <span style="font-size:28px">${icon}</span>
        <div>
          <h3 style="font-size:18px;font-weight:700;margin:0">${(PLATFORMS_META||{})[platform]||platform}</h3>
          <div style="font-size:11px;color:var(--text-muted)">${slogan}</div>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span style="font-size:11px;padding:4px 10px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px">
          📱 在线 <b id="pg-online-${platform}" style="color:#22c55e">-</b> / <b id="pg-total-${platform}">-</b>
        </span>
        <span style="font-size:11px;padding:4px 10px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px">
          🔄 执行中 <b id="pg-running-${platform}" style="color:#f59e0b">-</b>
        </span>
        <button class="qa-btn" onclick="loadPlatGridPage('${platform}')" style="font-size:11px;padding:4px 10px">⟳ 刷新</button>
      </div>
    </div>

    <!-- 设备卡片网格 -->
    <div id="pg-device-grid-${platform}" class="pg-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(185px,1fr));gap:10px;margin-bottom:14px">
      ${Array(6).fill(0).map(()=>'<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:12px;min-height:90px"><div style="height:12px;background:rgba(255,255,255,.06);border-radius:4px;margin-bottom:8px;width:60%"></div><div style="height:8px;background:rgba(255,255,255,.04);border-radius:4px;width:80%"></div></div>').join('')}
    </div>

    <!-- 快捷操作区 -->
    <div id="pg-quick-actions-${platform}" style="margin-bottom:14px"></div>

    <!-- 实战任务区 -->
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:14px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <div style="font-size:13px;font-weight:600">📋 实战任务</div>
        <div style="display:flex;gap:6px">
          <button class="qa-btn" onclick="loadPlatGridPage('${platform}')" style="padding:4px 10px;font-size:11px">↺ 刷新</button>
          <button class="qa-btn" onclick="navigateToPage('tasks')" style="padding:4px 10px;font-size:11px">全部 →</button>
        </div>
      </div>
      <div id="plat-recent-${platform}" style="font-size:12px;color:var(--text-muted)">加载中...</div>
    </div>

    <!-- 设备详情侧滑面板 -->
    <div id="pg-panel-${platform}" class="pg-panel">
      <div id="pg-panel-content-${platform}"></div>
    </div>
    <div id="pg-overlay-${platform}" class="pg-overlay" onclick="_pgClosePanel('${platform}')"></div>
  `;
}

// PLATFORMS_META 元数据（display name）
const PLATFORMS_META = {telegram:'Telegram',whatsapp:'WhatsApp',facebook:'Facebook',linkedin:'LinkedIn',instagram:'Instagram',twitter:'X (Twitter)'};

// ═══════════════════════════════════════════════════
// 渲染设备卡片网格
// ═══════════════════════════════════════════════════
function _pgRenderGrid(platform, devices) {
  const grid = document.getElementById(`pg-device-grid-${platform}`);
  if (!grid) return;

  if (!devices.length) {
    grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;color:var(--text-muted);padding:24px;font-size:13px">未发现设备 — 请检查 ADB 连接</div>';
    return;
  }

  const color = PLAT_COLORS[platform] || '#60a5fa';
  grid.innerHTML = devices.map(dev => _pgRenderCard(dev, platform, color)).join('');
}

function _pgRenderCard(dev, platform, color) {
  const alias = dev.alias || dev.device_id.substring(0, 8);
  const online = dev.online;
  const dotColor = online ? '#22c55e' : '#ef4444';
  const nodeLabel = dev.host || '主控';
  const nodeColor = _pgNodeColor(nodeLabel);

  let state = 'offline';
  if (online && dev.running_task) state = 'busy';
  else if (online && dev.today_tasks > 0) state = 'online';
  else if (online) state = 'idle';

  const taskTag = dev.running_task
    ? `<span class="pg-task-tag" style="background:${color}22;color:${color}">${TASK_NAMES[dev.running_task]||dev.running_task.replace(platform+'_','')}</span>`
    : (online ? '<span class="pg-task-tag" style="color:var(--text-dim)">😴 空闲</span>' : '<span class="pg-task-tag" style="color:#ef4444">离线</span>');

  const cfg = PLAT_GRID_CFG[platform] || {};
  const statItems = (cfg.stats||[]).map(s => {
    const v = dev[s.key] ?? 0;
    return `<span style="${v===0?'opacity:.4':''}">${s.icon}<b>${v}</b></span>`;
  }).join('');

  return `<div class="pg-card state-${state}" data-did="${dev.device_id}" onclick="_pgOpenPanel('${platform}','${dev.device_id}')">
    <div class="pg-card-head">
      <span class="pg-dot" style="background:${dotColor}"></span>
      <span class="pg-alias">${alias}</span>
      <span class="pg-node" style="color:${nodeColor};background:${nodeColor}18">${nodeLabel}</span>
      <span style="margin-left:auto">${taskTag}</span>
    </div>
    <div class="pg-stat-row">${statItems}</div>
    ${dev.algo_score > 0 ? `<div style="display:flex;align-items:center;gap:6px;font-size:9px;color:var(--text-muted)"><span>⚡ ${dev.algo_score}</span><div style="flex:1;height:3px;background:var(--border);border-radius:2px"><div style="height:100%;width:${Math.min(100,dev.algo_score)}%;background:${dev.algo_score>=60?'#22c55e':dev.algo_score>=30?'#f59e0b':'#94a3b8'};border-radius:2px"></div></div></div>` : ''}
  </div>`;
}

function _pgNodeColor(label) {
  if (label.startsWith('W')) return '#60a5fa';
  if (label === '主控') return '#94a3b8';
  return '#a78bfa';
}

// ═══════════════════════════════════════════════════
// 渲染快捷操作（复用 PLAT_PHASES）
// ═══════════════════════════════════════════════════
function _pgRenderQuickActions(platform, taskTypes) {
  const el = document.getElementById(`pg-quick-actions-${platform}`);
  if (!el) return;

  const phases = PLAT_PHASES[platform] || [];
  if (!phases.length) return;

  const icon = PLAT_ICONS[platform] || '';

  el.innerHTML = '<div style="font-size:13px;font-weight:600;margin-bottom:10px">快捷操作</div>' +
    '<div style="display:flex;flex-direction:column;gap:10px">' +
    phases.map(phase => {
      const phaseTasks = taskTypes.filter(t => phase.types.includes(t.type));
      if (!phaseTasks.length) return '';
      const phaseRunning = phase.types.reduce((a,tp) => a + (_platRunningTasks[tp]||0), 0);
      return `<div style="background:var(--bg-card);border:1px solid ${phase.highlight?phase.color+'44':'var(--border)'};border-radius:10px;padding:10px 12px;${phase.highlight?'box-shadow:0 0 0 1px '+phase.color+'22':''}">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <span style="font-size:11px;font-weight:700;color:${phase.color}">${phase.label}</span>
          <span style="font-size:10px;color:var(--text-dim)">${phase.desc}</span>
          ${phaseRunning>0?`<span style="margin-left:auto;font-size:10px;background:${phase.color}22;color:${phase.color};border-radius:4px;padding:1px 6px;font-weight:600">${phaseRunning}台运行中</span>`:''}
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px">
          ${phaseTasks.map(t => {
            const running = _platRunningTasks[t.type] || 0;
            const tIcon = PLAT_TASK_ICONS[t.type] || icon;
            const tGrad = PLAT_TASK_GRADIENTS[t.type] || ('linear-gradient(135deg,'+phase.color+','+phase.color+'dd)');
            const hint = PLAT_TASK_HINTS[t.type] || '';
            const isRunning = running > 0;
            return `<div class="action-card${isRunning?' action-card-running':''}" onclick="_tkQuickTaskWithModal('${platform}','${t.type}')" style="position:relative;${isRunning?'border-color:'+phase.color+';':''}" title="${hint}">
              <div class="action-icon" style="background:${tGrad}">${tIcon}</div>
              <div class="action-label">${t.label}</div>
              <div style="font-size:10px;color:var(--text-dim);margin-top:1px">${hint}</div>
              <div class="action-desc">${isRunning?'<span style="color:#f59e0b;font-weight:600">🔄 '+running+'台执行中</span>':'○ 空闲'}</div>
            </div>`;
          }).join('')}
        </div>
      </div>`;
    }).join('') +
    '</div>';

  // 加载任务列表
  _loadPlatRecentTasks(platform);
}

// ═══════════════════════════════════════════════════
// 设备详情侧滑面板
// ═══════════════════════════════════════════════════
function _pgOpenPanel(platform, deviceId) {
  const panel = document.getElementById(`pg-panel-${platform}`);
  const overlay = document.getElementById(`pg-overlay-${platform}`);
  const content = document.getElementById(`pg-panel-content-${platform}`);
  if (!panel || !content) return;

  const cached = _pgCache[platform];
  const dev = (cached?.devices || []).find(d => d.device_id === deviceId) || {device_id: deviceId};
  content.innerHTML = _pgBuildPanel(platform, dev);
  panel.classList.add('open');
  if (overlay) overlay.style.display = 'block';
}

function _pgClosePanel(platform) {
  const panel = document.getElementById(`pg-panel-${platform}`);
  const overlay = document.getElementById(`pg-overlay-${platform}`);
  if (panel) panel.classList.remove('open');
  if (overlay) overlay.style.display = 'none';
}

function _pgBuildPanel(platform, dev) {
  const alias = dev.alias || dev.device_id.substring(0, 8);
  const online = dev.online;
  const dotColor = online ? '#22c55e' : '#ef4444';
  const color = PLAT_COLORS[platform] || '#60a5fa';
  const icon = PLAT_ICONS[platform] || '';
  const cfg = PLAT_GRID_CFG[platform] || {};

  // 智能建议
  let suggestion = '';
  if (!online) {
    suggestion = '<div style="background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.25);border-radius:6px;padding:8px 10px;font-size:11px;color:#f87171;margin-bottom:12px">⚠ 设备离线，请检查 ADB 连接</div>';
  } else if (dev.today_tasks === 0) {
    suggestion = '<div style="background:rgba(99,102,241,.1);border:1px solid rgba(99,102,241,.25);border-radius:6px;padding:8px 10px;font-size:11px;color:#818cf8;margin-bottom:12px">📋 今日尚未在此平台执行任务 — 点下方按钮开始</div>';
  } else if (dev.running_task) {
    suggestion = `<div style="background:rgba(59,130,246,.1);border:1px solid rgba(59,130,246,.25);border-radius:6px;padding:8px 10px;font-size:11px;color:#60a5fa;margin-bottom:12px">🔄 正在执行: ${TASK_NAMES[dev.running_task]||dev.running_task}</div>`;
  }

  // 统计格子
  const statBoxes = (cfg.stats||[]).map(s => {
    const v = dev[s.key] ?? '-';
    return `<div style="text-align:center;padding:8px;background:var(--bg-main);border:1px solid var(--border);border-radius:8px;min-width:60px">
      <div style="font-size:18px;font-weight:700;color:var(--text)">${v}</div>
      <div style="font-size:9px;color:var(--text-muted);margin-top:2px">${s.icon} ${s.label}</div>
    </div>`;
  }).join('');

  // 快捷操作按钮
  const quickBtns = (cfg.quickActions || []).map(taskType => {
    const tIcon = PLAT_TASK_ICONS[taskType] || '▶';
    const label = TASK_NAMES[taskType] || taskType.replace(platform+'_','');
    return `<button onclick="_pgPanelTask('${platform}','${taskType}','${dev.device_id}');event.stopPropagation()"
      style="flex:1;min-width:80px;padding:8px 6px;font-size:11px;background:${color}15;border:1px solid ${color}30;color:${color};border-radius:6px;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:2px">
      <span style="font-size:16px">${tIcon}</span>${label}
    </button>`;
  }).join('');

  // 配置执行流程入口 —— 通用探测: 任何平台只要定义了 window[platform + 'DeviceConfigFlow']
  // 或 window[platform + 'OpenPresetsModal']，就自动显示主按钮，风格对齐 TikTok 侧栏。
  // 目前 facebook-ops.js 已实现 fbDeviceConfigFlow(deviceId)。TikTok 走 overview.js 专属面板，
  // 不经过本通用面板，因此在这里不重复展示。
  const flowFnName = (platform === 'facebook') ? 'fbDeviceConfigFlow' : (platform + 'DeviceConfigFlow');
  const hasFlowEntry = platform !== 'tiktok' && (
    typeof window[flowFnName] === 'function' ||
    typeof window[platform + 'OpenPresetsModal'] === 'function'
  );
  const flowBtnHtml = hasFlowEntry
    ? `<button onclick="_pgOpenFlowConfig('${platform}','${dev.device_id}');event.stopPropagation()"
         ${online ? '' : 'disabled'}
         title="${online ? '选择执行方案 + 目标市场，一键下发到该设备' : '设备离线，无法配置执行流程'}"
         style="width:100%;margin-bottom:14px;padding:10px 14px;font-size:13px;font-weight:600;
                background:linear-gradient(135deg,${color},${color}cc);
                border:none;color:#fff;border-radius:8px;cursor:${online?'pointer':'not-allowed'};
                opacity:${online?'1':'0.5'};display:flex;align-items:center;justify-content:center;gap:8px;
                box-shadow:0 2px 8px ${color}55">
         <span style="font-size:15px">⚡</span>配置执行流程
         <span style="font-size:10px;opacity:.8;margin-left:4px">选方案 · 选市场 · 一键启动</span>
       </button>`
    : '';

  return `
    <div style="padding:16px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
        <div style="display:flex;align-items:center;gap:8px">
          <span style="width:10px;height:10px;border-radius:50%;background:${dotColor};${online?'box-shadow:0 0 6px '+dotColor:''}"></span>
          <span style="font-size:16px;font-weight:700">${alias}</span>
          <span style="font-size:10px;padding:2px 8px;background:${color}18;color:${color};border-radius:4px;font-weight:600">${icon} ${PLATFORMS_META[platform]||platform}</span>
        </div>
        <button onclick="_pgClosePanel('${platform}')" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:20px">✕</button>
      </div>

      ${suggestion}

      <!-- 统计概览 -->
      <div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap">${statBoxes}</div>

      ${flowBtnHtml}

      <!-- 快捷操作 -->
      <div style="font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:8px">快捷操作</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px">${quickBtns}</div>

      <!-- 设备信息 -->
      <div style="font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:6px">设备信息</div>
      <div style="font-size:11px;color:var(--text-muted);background:var(--bg-main);border:1px solid var(--border);border-radius:6px;padding:10px;margin-bottom:14px">
        <div style="display:flex;justify-content:space-between;margin-bottom:4px"><span>设备 ID</span><span style="color:var(--text);font-family:monospace;font-size:10px">${dev.device_id.substring(0,16)}</span></div>
        <div style="display:flex;justify-content:space-between;margin-bottom:4px"><span>节点</span><span style="color:var(--text)">${dev.host||'主控'}</span></div>
        <div style="display:flex;justify-content:space-between;margin-bottom:4px"><span>状态</span><span style="color:${online?'#22c55e':'#ef4444'}">${online?'在线':'离线'}</span></div>
        ${dev.phase?`<div style="display:flex;justify-content:space-between"><span>阶段</span><span style="color:var(--text)">${dev.phase}</span></div>`:''}
      </div>

      <!-- 全部任务类型 -->
      <div style="font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:6px">全部任务</div>
      <div style="display:flex;flex-direction:column;gap:4px">
        ${(_pgCache[platform]?.task_types||[]).map(t => {
          const tIcon = PLAT_TASK_ICONS[t.type] || '▶';
          return `<button onclick="_pgPanelTask('${platform}','${t.type}','${dev.device_id}');event.stopPropagation()"
            style="display:flex;align-items:center;gap:8px;padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);border-radius:6px;cursor:pointer;font-size:11px;color:var(--text);text-align:left;width:100%">
            <span style="font-size:14px">${tIcon}</span>${t.label}
            <span style="margin-left:auto;font-size:9px;color:var(--text-dim)">${PLAT_TASK_HINTS[t.type]||''}</span>
          </button>`;
        }).join('')}
      </div>
    </div>
  `;
}

// 从面板内打开"配置执行流程"弹窗 —— 通用路由
// 优先级: window[platform+'DeviceConfigFlow'] > facebook 专属别名 fbDeviceConfigFlow
//       > window[platform+'OpenPresetsModal'] (兜底)
function _pgOpenFlowConfig(platform, deviceId) {
  const fbAlias = (platform === 'facebook') ? window.fbDeviceConfigFlow : null;
  const fn = window[platform + 'DeviceConfigFlow'] || fbAlias || window[platform + 'OpenPresetsModal'];
  if (typeof fn !== 'function') {
    showToast('该平台暂未实现「配置执行流程」', 'warning');
    return;
  }
  try {
    _pgClosePanel(platform);
    fn(deviceId);
  } catch (e) {
    showToast('打开执行流程失败: ' + (e.message || e), 'error');
  }
}

// 从面板内发起单设备任务
async function _pgPanelTask(platform, taskType, deviceId) {
  const paramDefs = PLAT_TK_PARAMS[taskType] || [];
  const needsAudienceModal = platform === 'tiktok' && typeof platTkNeedsAudienceModal === 'function' && platTkNeedsAudienceModal(taskType);
  if (paramDefs.length || taskType.includes('send_message') || taskType.includes('send_dm') || needsAudienceModal) {
    _pgClosePanel(platform);
    await _tkQuickTaskWithModal(platform, taskType);
    return;
  }
  try {
    await api('POST', `/platforms/${platform}/tasks`, {task_type: taskType, device_id: deviceId, params: {}});
    const alias = ALIAS[deviceId] || deviceId.substring(0,8);
    showToast(`✓ ${alias}: ${TASK_NAMES[taskType]||taskType} 任务已创建`, 'success');
    setTimeout(() => loadPlatGridPage(platform), 1000);
  } catch(e) {
    showToast('创建失败: ' + (e.message||''), 'error');
  }
}

// ═══════════════════════════════════════════════════
// 自动刷新（30秒）
// ═══════════════════════════════════════════════════
function _pgStartAutoRefresh(platform) {
  if (_pgRefreshTimers[platform]) clearInterval(_pgRefreshTimers[platform]);
  _pgRefreshTimers[platform] = setInterval(async () => {
    if (_currentPage !== 'plat-' + platform) {
      clearInterval(_pgRefreshTimers[platform]);
      return;
    }
    try {
      const [gridData] = await Promise.all([
        api('GET', `/platforms/${platform}/device-grid`),
        _fetchPlatTasks(platform),
      ]);
      _pgCache[platform] = gridData;
      const s = gridData.summary || {};
      _pgSetText(`pg-online-${platform}`, s.online || 0);
      _pgSetText(`pg-total-${platform}`, s.total || 0);
      _pgSetText(`pg-running-${platform}`, s.running_tasks || 0);
      _pgRenderGrid(platform, gridData.devices || []);
      _loadPlatRecentTasks(platform);
    } catch(e) {}
  }, 30000);
}

function _pgSetText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}
