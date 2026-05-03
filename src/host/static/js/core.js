/* core.js — 基础设施: API代理、i18n、Auth Guard、User Management、Sidebar、Toast、Theme */
/* ── 反代 / 分离端口：localhost 上常见前端端口 3000 / 5173 / 4173 会自动把 API/WS 指到同主机 :8000。
   其它环境可手动: localStorage.setItem('oc_api_origin','http://IP:8000'); 或 oc_ws_root=ws://IP:8000 */
function _apiOrigin(){
  try{
    const s=(localStorage.getItem('oc_api_origin')||'').trim().replace(/\/$/,'');
    if(s) return s;
  }catch(e){}
  const h=location.hostname,p=location.port;
  const devFront=['3000','5173','4173'];
  if((h==='localhost'||h==='127.0.0.1')&&devFront.indexOf(p)>=0){
    return 'http://'+h+':8000';
  }
  return '';
}
function _apiUrl(path){
  if(!path)return path;
  if(path.indexOf('http://')===0||path.indexOf('https://')===0)return path;
  const b=_apiOrigin();
  return b?b+path:path;
}
function _wsUrl(path){
  if(!path||path.charAt(0)!=='/')path='/'+(path||'');
  let wr='';
  try{wr=(localStorage.getItem('oc_ws_root')||'').trim();}catch(e){}
  if(wr){const r=wr.replace(/\/$/,'');return r+path;}
  const b=_apiOrigin();
  if(b){try{const u=new URL(b);const pr=u.protocol==='https:'?'wss:':'ws:';return pr+'//'+u.host+path;}catch(e){}}
  const proto=location.protocol==='https:'?'wss:':'ws:';
  return proto+'//'+location.host+path;
}

/* ── i18n 国际化 ── */
const _i18n={
  zh:{
    'overview':'总览','devices':'设备管理','tasks':'任务管理','screen-monitor':'屏幕监控',
    'batch-ops':'批量操作','ai-assistant':'AI 助手','cluster':'集群管理','platforms':'平台控制',
    'perf-monitor':'性能监控','screen-record':'录屏管理','script-engine':'脚本执行器',
    'quick-actions':'批量快捷操作','batch-upload':'批量文件上传','scheduled-jobs':'定时任务',
    'data-export':'数据导出','device-assets':'设备资产','ai-script':'AI脚本生成',
    'op-timeline':'操作时间线','sync-mirror':'同步镜像操作','health-report':'健康报告',
    'tpl-market':'模板市场','visual-workflow':'可视化工作流','multi-screen':'多屏并行操控',
    'user-mgmt':'用户管理','logout':'退出登录','api-docs':'API 文档',
    'system-status':'系统状态','running':'运行正常','online-devices':'在线设备',
    'total-devices':'总设备','total-tasks':'总任务','success':'成功','failed':'失败',
    'running-tasks':'执行中','search-device':'搜索设备…','all-status':'全部状态',
    'online':'在线','offline':'离线','busy':'执行中','compact':'紧凑','standard':'标准',
    'large-card':'大卡片','save':'保存','load':'加载','execute':'执行','clear':'清空',
    'cancel':'取消','confirm':'确认','delete':'删除','create':'创建','refresh':'刷新',
  },
  en:{
    'overview':'Overview','devices':'Devices','tasks':'Tasks','screen-monitor':'Screen Monitor',
    'batch-ops':'Batch Ops','ai-assistant':'AI Assistant','cluster':'Cluster','platforms':'Platforms',
    'perf-monitor':'Performance','screen-record':'Recordings','script-engine':'Script Engine',
    'quick-actions':'Quick Actions','batch-upload':'File Upload','scheduled-jobs':'Scheduled Jobs',
    'data-export':'Data Export','device-assets':'Device Assets','ai-script':'AI Script Gen',
    'op-timeline':'Timeline','sync-mirror':'Sync Mirror','health-report':'Health Report',
    'tpl-market':'Templates','visual-workflow':'Visual Workflow','multi-screen':'Multi-Screen',
    'user-mgmt':'Users','logout':'Logout','api-docs':'API Docs',
    'system-status':'System Status','running':'Running','online-devices':'Online','total-devices':'Total',
    'total-tasks':'Tasks','success':'Success','failed':'Failed','running-tasks':'Running',
    'search-device':'Search devices...','all-status':'All Status',
    'online':'Online','offline':'Offline','busy':'Busy','compact':'Compact','standard':'Standard',
    'large-card':'Large','save':'Save','load':'Load','execute':'Execute','clear':'Clear',
    'cancel':'Cancel','confirm':'Confirm','delete':'Delete','create':'Create','refresh':'Refresh',
  }
};
let _curLang=localStorage.getItem('oc-lang')||'zh';
function t(key){return (_i18n[_curLang]&&_i18n[_curLang][key])||(_i18n.zh[key])||key;}
function toggleLang(){
  _curLang=_curLang==='zh'?'en':'zh';
  localStorage.setItem('oc-lang',_curLang);
  document.getElementById('lang-toggle').textContent=_curLang==='zh'?'中':'EN';
  _applyI18n();
}
function _applyI18n(){
  document.querySelectorAll('[data-i18n]').forEach(el=>{
    el.textContent=t(el.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el=>{
    el.placeholder=t(el.dataset.i18nPlaceholder);
  });
}
(function _initLang(){
  const btn=document.getElementById('lang-toggle');
  if(btn)btn.textContent=_curLang==='zh'?'中':'EN';
})();

/* ── Worker Color Registry ─────────────────────────────────────────── */
// Palette excludes green/red to avoid confusion with online/offline status indicators
const _WCP=['#2196F3','#9C27B0','#FF9800','#00BCD4','#FF5722','#795548','#E91E63','#607D8B'];
window._workerColorMap={};
window._deviceWorkerMap={}; // device_id → workerName, populated after loadDevices

function _getWorkerColor(name){
  if(!name||name==='本机'||name==='主控')return '#607D8B';
  if(!_workerColorMap[name]){
    // Stable hash: same name always gets same color regardless of load order
    let h=0;for(let i=0;i<name.length;i++)h=(h*31+name.charCodeAt(i))&0xffff;
    _workerColorMap[name]=_WCP[h%_WCP.length];
  }
  return _workerColorMap[name];
}
function _hexToRgba(hex,a){
  try{const r=parseInt(hex.slice(1,3),16),g=parseInt(hex.slice(3,5),16),b=parseInt(hex.slice(5,7),16);return `rgba(${r},${g},${b},${a})`;}
  catch(e){return `rgba(96,165,250,${a})`;}
}
function _getDeviceWorker(device_id){return window._deviceWorkerMap[device_id]||'本机';}
function _workerBadge(workerName){
  if(!workerName||workerName==='本机'||workerName==='主控')return '';
  const c=_getWorkerColor(workerName);
  return `<span class="worker-badge" style="background:${c}">${workerName}</span>`;
}
function _workerBadgeById(device_id){
  const wn=_getDeviceWorker(device_id);
  return wn!=='本机'?_workerBadge(wn):'';
}
function _buildDeviceWorkerMap(devices){
  (devices||[]).forEach(d=>{
    window._deviceWorkerMap[d.device_id]=(d._isCluster&&d.host_name)?d.host_name:'本机';
  });
}

/* ── Auth Guard ── */
const _OC_TOKEN=localStorage.getItem('oc_token')||'';
const _OC_USER=localStorage.getItem('oc_user')||'';
const _OC_ROLE=localStorage.getItem('oc_role')||'';
function _authHeaders(){return _OC_TOKEN?{'Authorization':'Bearer '+_OC_TOKEN}:{}}
(function _authGuard(){
  if(!_OC_TOKEN){window.location.href='/login';return;}
  const ui=document.getElementById('user-info');
  if(ui)ui.textContent=(_OC_USER||'user')+' ('+(_OC_ROLE==='admin'?'管理员':_OC_ROLE==='operator'?'操作员':'只读')+')';
})();
function doLogout(){
  fetch(_apiUrl('/auth/logout'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:_OC_TOKEN})});
  localStorage.removeItem('oc_token');localStorage.removeItem('oc_user');localStorage.removeItem('oc_role');
  document.cookie='oc_token=;path=/;max-age=0';window.location.href='/login';
}

/* ── User Management ── */
async function loadUserMgmtPage(){
  const list=document.getElementById('users-list');
  if(!list)return; list.innerHTML='<div style="color:var(--text-muted)">加载中…</div>';
  try{
    const r=await fetch(_apiUrl('/auth/users'),{headers:_authHeaders()});
    const users=await r.json();
    list.innerHTML=users.map(u=>`
      <div style="display:flex;align-items:center;justify-content:space-between;background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:12px 16px">
        <div><span style="font-weight:600;font-size:13px">${u.username}</span>
        <span style="margin-left:8px;font-size:10px;padding:2px 8px;border-radius:6px;background:${u.role==='admin'?'#3b82f6':u.role==='operator'?'#22c55e':'#94a3b8'};color:#fff">${u.role==='admin'?'管理员':u.role==='operator'?'操作员':'只读'}</span>
        <span style="margin-left:8px;font-size:11px;color:var(--text-muted)">${u.display||''}</span></div>
        <div style="display:flex;gap:6px">
          <button class="sb-btn2" onclick="editUserRole('${u.username}')" style="font-size:10px">改角色</button>
          <button class="sb-btn2" onclick="resetUserPass('${u.username}')" style="font-size:10px">重置密码</button>
          <button class="sb-btn2" onclick="deleteUser('${u.username}')" style="font-size:10px;color:#f87171">删除</button>
        </div>
      </div>`).join('');
  }catch(e){list.innerHTML='<div style="color:#f87171">加载失败:'+e.message+'</div>';}
}
function showAddUserForm(){document.getElementById('add-user-form').style.display='block';}
async function createUser(){
  const name=document.getElementById('new-user-name').value.trim();
  const pass=document.getElementById('new-user-pass').value;
  const role=document.getElementById('new-user-role').value;
  const disp=document.getElementById('new-user-display').value.trim();
  if(!name)return alert('请输入用户名');
  await fetch(_apiUrl('/auth/users'),{method:'POST',headers:{...{'Content-Type':'application/json'},..._authHeaders()},
    body:JSON.stringify({username:name,password:pass,role:role,display:disp||name})});
  document.getElementById('add-user-form').style.display='none';
  loadUserMgmtPage();
}
async function editUserRole(username){
  const role=prompt('输入新角色 (admin / operator / viewer):','operator');
  if(!role)return;
  await fetch(_apiUrl('/auth/users/'+username),{method:'PUT',headers:{...{'Content-Type':'application/json'},..._authHeaders()},
    body:JSON.stringify({role:role})});
  loadUserMgmtPage();
}
async function resetUserPass(username){
  const pass=prompt('输入新密码:','123456');
  if(!pass)return;
  await fetch(_apiUrl('/auth/users/'+username),{method:'PUT',headers:{...{'Content-Type':'application/json'},..._authHeaders()},
    body:JSON.stringify({password:pass})});
  alert('密码已重置');
}
async function deleteUser(username){
  if(!confirm('确认删除用户 '+username+'?'))return;
  await fetch(_apiUrl('/auth/users/'+username),{method:'DELETE',headers:_authHeaders()});
  loadUserMgmtPage();
}
async function changeMyPassword(){
  const pass=document.getElementById('change-pass').value;
  if(!pass)return alert('请输入新密码');
  await fetch(_apiUrl('/auth/users/'+_OC_USER),{method:'PUT',headers:{...{'Content-Type':'application/json'},..._authHeaders()},
    body:JSON.stringify({password:pass})});
  alert('密码修改成功，下次登录生效');
}
function showUserMenu(){showPage('user-mgmt');}

/* ── Sidebar Section Toggle & Search ── */
function _toggleSection(el){
  el.classList.toggle('collapsed');
  const grp=el.nextElementSibling;
  if(grp&&grp.classList.contains('nav-group')){grp.classList.toggle('collapsed');}
}
function _filterNav(q){
  q=q.toLowerCase().trim();
  document.querySelectorAll('.nav-group .nav-item').forEach(it=>{
    const txt=(it.textContent||'').toLowerCase();
    const pg=(it.dataset.page||'').toLowerCase();
    const match=!q||txt.includes(q)||pg.includes(q);
    it.classList.toggle('hidden',!match);
  });
  if(q){
    document.querySelectorAll('.nav-group').forEach(g=>{g.classList.remove('collapsed');});
    document.querySelectorAll('.nav-section').forEach(s=>{s.classList.remove('collapsed');});
  }
}
document.addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&e.key==='k'){e.preventDefault();const s=document.getElementById('nav-search');if(s){s.focus();s.select();}}
});

/* ── Toast Notification System ── */
function showToast(msg,type='info',duration=4000,group=''){
  const icons={success:'\u2705',error:'\u274C',warn:'\u26A0\uFE0F',info:'\u2139\uFE0F'};
  const c=document.getElementById('toast-container');
  // 同组 toast 互斥：新消息自动清除旧消息
  if(group){
    c.querySelectorAll('[data-toast-group="'+group+'"]').forEach(el=>el.remove());
  }
  const t=document.createElement('div');
  t.className='toast '+(type||'info');
  if(group) t.setAttribute('data-toast-group',group);
  t.innerHTML='<span class="t-icon">'+(icons[type]||icons.info)+'</span><span class="t-msg">'+msg+'</span><span class="t-close" onclick="this.parentElement.remove()">&times;</span>';
  c.appendChild(t);
  setTimeout(()=>{if(t.parentElement)t.remove();},duration);
}

/* ── Theme ── */
function _initTheme(){
  const saved=localStorage.getItem('oc-theme');
  if(saved){document.documentElement.setAttribute('data-theme',saved);}
  else if(window.matchMedia('(prefers-color-scheme:light)').matches){document.documentElement.setAttribute('data-theme','light');}
  _updateThemeIcon();
}
function toggleTheme(){
  const cur=document.documentElement.getAttribute('data-theme')||'dark';
  const next=cur==='dark'?'light':'dark';
  document.documentElement.setAttribute('data-theme',next);
  localStorage.setItem('oc-theme',next);
  _updateThemeIcon();
}
function _updateThemeIcon(){
  const btn=document.getElementById('theme-toggle');
  if(!btn) return;
  const isDark=(document.documentElement.getAttribute('data-theme')||'dark')==='dark';
  btn.textContent=isDark?'\u{1F319}':'\u{2600}\u{FE0F}';
}
_initTheme();

let ALIAS={};
window.WP_NUM = {};       // {device_id: wallpaper_number} 壁纸状态追踪
window._globalRanges = {}; // {host_id: {start,end}} 全局编号段配置

async function loadAliases(){
  try{
    const data=await api('GET','/devices/aliases');
    ALIAS={};
    window.WP_NUM={};
    for(const[did,info] of Object.entries(data)){
      ALIAS[did]=info.alias||`${(info.number||0).toString().padStart(2,'0')}号`;
      if(info.wallpaper_number) window.WP_NUM[did]=info.wallpaper_number;
    }
  }catch(e){ALIAS={}; window.WP_NUM={};}
  // 预加载全局编号段配置（供 _updateUnsetCount 和 devCard 使用）
  try{
    window._globalRanges = await api('GET','/cluster/number-ranges');
  }catch(e){ window._globalRanges={}; }
  // 刷新未编号计数徽章
  if(typeof _updateUnsetCount==='function') _updateUnsetCount();
}
// TASK_NAMES 是前端当前展示字典：后端可能返回历史任务创建时的旧 type_label_zh，
// 因此前端展示优先使用 TASK_NAMES，再回退到 type_label_zh。
// 本字典在启动时会被 refreshTaskLabels() 覆盖并保持 Object 引用不变（Object.assign）。
let TASK_NAMES={tiktok_warmup:'养号',tiktok_watch:'刷视频',tiktok_browse_feed:'刷视频',tiktok_follow:'关注',tiktok_test_follow:'测试关注',tiktok_send_dm:'发私信',tiktok_check_inbox:'查收件箱',tiktok_acquisition:'全流程获客',tiktok_auto:'全流程获客',tiktok_check_and_chat_followbacks:'回关私信',vpn_setup:'配置VPN',vpn_status:'VPN状态',telegram_send_message:'Telegram 发消息',telegram_read_messages:'Telegram 读消息',telegram_send_file:'Telegram 发文件',telegram_workflow:'Telegram 工作流',telegram_auto_reply:'Telegram 自动回复',telegram_join_group:'Telegram 加群',telegram_send_group:'Telegram 群消息',telegram_monitor_chat:'Telegram 监控',whatsapp_send_message:'WhatsApp 发消息',whatsapp_read_messages:'WhatsApp 读消息',whatsapp_auto_reply:'WhatsApp 自动回复',whatsapp_send_media:'WhatsApp 发媒体',whatsapp_list_chats:'WhatsApp 聊天列表',facebook_send_message:'Facebook 发私信',facebook_add_friend:'Facebook 加好友(安全)',facebook_browse_feed:'Facebook 浏览动态',facebook_browse_feed_by_interest:'Facebook 兴趣刷帖',facebook_search_leads:'Facebook 搜索潜客',facebook_join_group:'Facebook 加入群组',facebook_browse_groups:'Facebook 浏览我的群组',facebook_group_engage:'Facebook 群组互动',facebook_extract_members:'Facebook 群成员候选采集',facebook_group_member_greet:'Facebook 群成员好友打招呼',facebook_check_inbox:'Facebook Messenger 收件箱',facebook_check_message_requests:'Facebook 陌生人收件箱',facebook_check_friend_requests:'Facebook 好友请求处理',facebook_campaign_run:'Facebook 剧本任务',linkedin_send_message:'LinkedIn 发消息',linkedin_read_messages:'LinkedIn 读消息',linkedin_post_update:'LinkedIn 发动态',linkedin_search_profile:'LinkedIn 搜人脉',linkedin_send_connection:'LinkedIn 发邀请',linkedin_accept_connections:'LinkedIn 接受邀请',linkedin_like_post:'LinkedIn 点赞',linkedin_comment_post:'LinkedIn 评论',instagram_browse_feed:'Instagram 浏览首页',instagram_browse_hashtag:'Instagram 浏览标签',instagram_search_leads:'Instagram 搜用户',instagram_send_dm:'Instagram 发私信',twitter_browse_timeline:'X 浏览时间线',twitter_search_leads:'X 搜用户',twitter_search_and_engage:'X 关键词互动',twitter_send_dm:'X 发私信'};
window.TASK_NAMES = TASK_NAMES;
function applyBusinessSafeTaskNames(){
  TASK_NAMES.facebook_extract_members = 'Facebook 群成员候选采集';
  TASK_NAMES.facebook_group_member_greet = 'Facebook 群成员好友打招呼';
  TASK_NAMES.facebook_campaign_run = 'Facebook 剧本任务';
}
// 从后端拉取统一的 task_type -> 中文 字典，覆盖本地兜底。启动时调一次即可。
async function refreshTaskLabels(){
  try{
    const r = await api('GET','/tasks/meta/labels');
    const labels = (r && r.labels) || {};
    if(labels && typeof labels === 'object'){
      Object.assign(TASK_NAMES, labels);
      applyBusinessSafeTaskNames();
    }
  }catch(e){ /* 后端不可用时保持兜底字典即可 */ }
  applyBusinessSafeTaskNames();
}
applyBusinessSafeTaskNames();
window.businessSafeText = function(text){
  return String(text == null ? '' : text)
    .replace(/FB 提取群成员/g, 'FB 群成员候选采集')
    .replace(/Facebook 提取群成员/g, 'Facebook 群成员候选采集')
    .replace(/提取群成员/g, '群成员候选采集')
    .replace(/群成员提取/g, '群成员候选采集')
    .replace(/成员采集/g, '成员整理')
    .replace(/圈层拓客/g, '社群客服拓展')
    .replace(/好友拓展/g, '好友打招呼')
    .replace(/全链路获客/g, '全链路客服拓展');
};
// 统一入口：任务对象 -> 展示名（当前前端字典优先，历史 type_label_zh 只作回退）
window.taskDisplayName = function(task){
  if(!task) return '';
  const t = (task.type && TASK_NAMES[task.type]) || task.type_label_zh || task.type || '';
  return businessSafeText(t);
};
refreshTaskLabels();
let allDevices=[], allTasks=[], currentFilter='all';
let modalDeviceId=null, modalTimer=null, allRefreshTimer=null;
let _wsConnected=false;
let _devicePerfCache={};

let _loadDevicesDebounce=null;
function _scheduleLoadDevices(){
  if(_loadDevicesDebounce) clearTimeout(_loadDevicesDebounce);
  _loadDevicesDebounce=setTimeout(()=>{loadDevices().catch(()=>{});_loadDevicesDebounce=null;},450);
}
async function api(method,path,body,timeoutMs){
  const headers={..._authHeaders()};
  if(body) headers['Content-Type']='application/json';
  if(_OC_TOKEN) headers['Authorization']='Bearer '+_OC_TOKEN;
  const apiKey=localStorage.getItem('oc_api_key');
  if(apiKey) headers['X-API-Key']=apiKey;
  const o={method,headers,credentials:'include'};
  if(body) o.body=JSON.stringify(body);
  const ms=timeoutMs!=null?timeoutMs:(path.indexOf('/devices')===0||path==='/devices'?120000:90000);
  const ctrl=new AbortController();
  const to=setTimeout(()=>ctrl.abort(),ms);
  try{
    const r=await fetch(_apiUrl(path),{...o,signal:ctrl.signal});
    clearTimeout(to);
    if(!r.ok){
      const txt=await r.text().catch(()=>'');
      let detail=txt.substring(0,400);
      let detailObj=null;     // 结构化 detail，给需要逐字段处理的调用方用（如 P1 fb-launch dialog）
      try{
        if(txt && txt.trim().charAt(0)==='{'){
          const j=JSON.parse(txt);
          const d=j&&j.detail;
          if(d&&typeof d==='object'){
            detailObj=d;
            const msg=(d.message||d.msg||d.error||'').trim();
            const hint=(d.hint||'').trim();
            const code=(d.code||'').trim();
            if(msg) detail=msg+(hint?(' — '+hint):'')+(code?(' ['+code+']'):'');
          }else if(typeof d==='string') detail=d;
        }
      }catch(_){}
      let errLine=`${r.status} ${r.statusText}: ${detail}`;
      if(r.status===404&&path&&(path.indexOf('install-apk')>=0||path.indexOf('install-apk-cluster')>=0)){
        errLine+=' [提示: 主控需已部署含集群 APK 转发的版本并已重启；反代须放行 /batch/install-apk-cluster 与 /cluster/batch/install-apk；可 GET /health 查看 capabilities]';
      }
      const err=new Error(errLine);
      // 增强属性 — 不影响既有 e.message 用法，让新代码能区分 422 等业务错误
      err.status=r.status;
      err.statusText=r.statusText;
      if(detailObj) err.detail=detailObj;
      throw err;
    }
    return await r.json();
  }catch(e){
    clearTimeout(to);
    if(e.name==='AbortError') throw new Error('请求超时，请检查服务是否卡住或网络');
    throw e;
  }
}
loadAliases();
async function _refreshPerfCache(){
  try{
    const r=await api('GET','/devices/performance/all');
    _devicePerfCache=r.devices||{};
  }catch(e){}
}
_refreshPerfCache();
setInterval(_refreshPerfCache,120000);
// 每2分钟自动刷新设备列表，感知新接入设备
setInterval(()=>{ if(typeof loadDevices==='function') loadDevices().catch(()=>{}); }, 120000);

/* VPN 管理已迁移到工具面板 → devices.js */

