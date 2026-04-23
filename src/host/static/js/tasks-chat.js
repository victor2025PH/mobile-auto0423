/* tasks-chat.js — 任务与聊天: Tasks、Chat、智能诊断（hint_message 优先时不再叠加热门关键字建议） */
/* ── Tasks ── */
const TASK_STEPS={
  'tiktok_warmup':['启动应用','浏览首页','观看视频','点赞互动','完成预热'],
  'tiktok_follow':['搜索用户','浏览主页','执行关注','验证成功'],
  'tiktok_check_inbox':['打开收件箱','读取消息','AI分析意图','生成回复','发送消息'],
  'tiktok_auto':['预热账号','搜索用户','执行关注','检查收件箱','AI自动回复'],
  'tiktok_check_and_chat_followbacks':['获取回关列表','分析用户','发送问候','跟进对话'],
  'tiktok_ai_rescore':['连接AI服务','扫描线索','重新评分','写回数据库'],
  'tiktok_ai_restore':['连接AI服务','扫描线索','重新评分','写回数据库'],
  'telegram_send':['连接Telegram','定位联系人','发送消息','确认发送'],
  'whatsapp_send':['打开WhatsApp','搜索联系人','输入消息','发送确认'],
};

function _getStepDesc(taskType, progress){
  const steps=TASK_STEPS[taskType];
  if(!steps||progress==null)return '';
  const idx=Math.min(Math.floor(progress/100*steps.length),steps.length-1);
  return steps[idx]||'';
}

function _taskParamsDisplay(params){
  if(!params||typeof params!=='object')return {};
  try{
    return Object.fromEntries(Object.entries(params).filter(function(ent){return ent[0]&&!String(ent[0]).startsWith('_');}));
  }catch(e){return{};}
}

/** 与 DELETE /tasks/{id} 一致：运行中/等待中不可删，须先取消 */
function _taskRecordDeletable(status){
  return status!=='running'&&status!=='pending';
}

/** 列表数据：回收站 / 带 status 的请求 / 「全部」合并结果均由 loadTasks 拉好，此处不再前端二次筛状态（避免与后端不一致） */
function _tasksFilteredForView(){
  return allTasks;
}

/** 任务中心多选（仅终态可勾选；回收站页全部可选） */
let _taskBulkSelected=new Set();

function _updateBulkDeleteBtn(){
  const del=document.getElementById('task-bulk-delete-btn');
  const rst=document.getElementById('task-bulk-restore-btn');
  const ers=document.getElementById('task-bulk-erase-btn');
  const trash=currentFilter==='trash';
  if(rst){
    rst.style.display=trash?'inline-block':'none';
    if(trash){
      const n=_taskBulkSelected.size;
      rst.disabled=n===0;
      rst.textContent=n?('恢复所选 ('+n+')'):'恢复所选';
    }
  }
  if(ers){
    ers.style.display=trash?'inline-block':'none';
    if(trash){
      const n=_taskBulkSelected.size;
      ers.disabled=n===0;
      ers.textContent=n?('永久删除所选 ('+n+')'):'永久删除所选';
    }
  }
  if(del){
    del.style.display=trash?'none':'inline-flex';
    if(!trash){
      const n=_taskBulkSelected.size;
      del.disabled=n===0;
      del.textContent=n?('移入所选 ('+n+')'):'移入所选';
    }
  }
  const fa=document.getElementById('task-failed-clear-all-btn');
  if(fa) fa.style.display=currentFilter==='failed'&&!trash?'inline-flex':'none';
}

function _syncTaskSelectAllCheckbox(){
  const el=document.getElementById('task-select-all');
  if(!el)return;
  const filtered=_tasksFilteredForView();
  const slice=filtered.slice(0,_taskDisplayLimit);
  const selectable=currentFilter==='trash'
    ?slice
    :slice.filter(function(t){return _taskRecordDeletable(t.status);});
  if(!selectable.length){el.checked=false;el.indeterminate=false;return;}
  let sel=0;
  selectable.forEach(function(t){if(_taskBulkSelected.has(t.task_id))sel++;});
  el.checked=sel===selectable.length;
  el.indeterminate=sel>0&&sel<selectable.length;
}

function taskBulkToggle(taskId,on){
  if(!taskId)return;
  if(on)_taskBulkSelected.add(taskId); else _taskBulkSelected.delete(taskId);
  _updateBulkDeleteBtn();
  _syncTaskSelectAllCheckbox();
}

function taskBulkToggleAll(checked){
  const filtered=_tasksFilteredForView();
  const slice=filtered.slice(0,_taskDisplayLimit);
  slice.forEach(function(t){
    const ok=currentFilter==='trash'||_taskRecordDeletable(t.status);
    if(ok){
      if(checked)_taskBulkSelected.add(t.task_id); else _taskBulkSelected.delete(t.task_id);
    }
  });
  renderTasks();
}

/** 集群合并可能短暂仍返回已软删 id，从当前列表剔除 */
function _pruneTaskIdsFromAllTasks(ids){
  if(!ids||!ids.length)return;
  const rm=new Set(ids);
  allTasks=allTasks.filter(function(t){return !rm.has(t.task_id);});
  renderTasks();
  _updateBulkDeleteBtn();
  _syncTaskSelectAllCheckbox();
}

const _TASK_STATUS_TABS=['running','pending','completed','failed','cancelled'];
/** 与 GET /tasks limit 上限一致；失败任务多时过低会导致「永远清不完一页」 */
const _TASK_LIST_LIMIT_STATUS=2000;
const _TASK_LIST_LIMIT_ALL=200;
const _TASK_FILTER_LABEL_ZH={all:'全部',running:'运行中',pending:'等待中',completed:'已完成',failed:'失败',cancelled:'已取消',trash:'回收站'};
/** 切换到「失败」标签时尝试自动展开错误分析（仅当次有效） */
let _eapAutoExpandPending=false;
/** 与 P9 错误分析面板一致，提前声明避免 loadTasks 早于面板段落执行时的 TDZ */
let _eapOpen=false;
let _eapDebounceTimer=null;

async function moveAllFailedDeletableToTrash(){
  if(typeof currentFilter==='undefined'||currentFilter!=='failed'){
    showToast('请先切换到「失败」标签','warn');
    return;
  }
  if(!confirm(
    '服务端将一次性把「失败」任务移入回收站：先本机批量 SQL，再通知集群各节点执行（比逐条删快得多）。\n\n确定继续？'
  ))return;
  try{
    const r=await api('POST','/tasks/trash-all-by-status?status=failed&forward_cluster=true');
    const loc=r.deleted_local!=null?r.deleted_local:0;
    const wk=r.deleted_on_workers!=null?r.deleted_on_workers:0;
    const tot=r.deleted_total!=null?r.deleted_total:(loc+wk);
    showToast('已处理失败任务 '+tot+' 条（本机 '+loc+' + 集群 '+wk+'）','success');
    await loadTasks();
    const remain=allTasks.filter(function(t){return t.status==='failed';}).length;
    if(remain===0) _flashOcTaskListCard();
    else showToast('列表仍显示约 '+remain+' 条，多为合并视图缓存，请点刷新或稍后再打开失败页','info',7000,'task-failed-clear');
    _taskBulkSelected.clear();
    document.getElementById('task-detail-modal')?.remove();
    _startTaskPoll();
  }catch(e){showToast('操作失败: '+e.message,'error');}
}

async function deleteSelectedTasksBulk(){
  const ids=[];
  _taskBulkSelected.forEach(function(id){
    const t=allTasks.find(function(x){return x.task_id===id;});
    if(t&&_taskRecordDeletable(t.status))ids.push(id);
  });
  if(!ids.length){showToast('没有可移入回收站的选中项（运行中/等待中须先取消）','warn');return;}
  if(!confirm('确定将 '+ids.length+' 条任务移入回收站？可在「回收站」恢复或永久删除。'))return;
  try{
    const r=await api('POST','/tasks/delete-batch',{task_ids:ids});
    const del=r.deleted||0;
    const sk=(r.skipped&&r.skipped.length)||0;
    let msg='已移入回收站 '+del+' 条';
    if(sk)msg+='（跳过 '+sk+' 条）';
    showToast(msg,'success');
    _taskBulkSelected.clear();
    document.getElementById('task-detail-modal')?.remove();
    await loadTasks();
    _pruneTaskIdsFromAllTasks(r.deleted_ids||[]);
    _startTaskPoll();
  }catch(e){showToast('批量操作失败: '+e.message,'error');}
}

async function restoreSelectedTasksBulk(){
  const ids=[];
  _taskBulkSelected.forEach(function(id){
    if(allTasks.some(function(x){return x.task_id===id;}))ids.push(id);
  });
  if(!ids.length){showToast('没有选中项','warn');return;}
  if(!confirm('确定恢复 '+ids.length+' 条任务到任务列表？'))return;
  try{
    const r=await api('POST','/tasks/restore-batch',{task_ids:ids});
    const n=r.restored||0;
    const sk=(r.skipped&&r.skipped.length)||0;
    let msg='已恢复 '+n+' 条';
    if(sk)msg+='（跳过 '+sk+' 条）';
    showToast(msg,'success');
    _taskBulkSelected.clear();
    document.getElementById('task-detail-modal')?.remove();
    await loadTasks();
  }catch(e){showToast('恢复失败: '+e.message,'error');}
}

async function eraseSelectedTasksBulk(){
  const ids=[];
  _taskBulkSelected.forEach(function(id){
    if(allTasks.some(function(x){return x.task_id===id;}))ids.push(id);
  });
  if(!ids.length){showToast('没有选中项','warn');return;}
  if(!confirm('永久删除 '+ids.length+' 条记录？此操作不可撤销。'))return;
  try{
    const r=await api('POST','/tasks/erase-batch',{task_ids:ids});
    const n=r.erased||0;
    const sk=(r.skipped&&r.skipped.length)||0;
    let msg='已永久删除 '+n+' 条';
    if(sk)msg+='（跳过 '+sk+' 条）';
    showToast(msg,'success');
    _taskBulkSelected.clear();
    document.getElementById('task-detail-modal')?.remove();
    await loadTasks();
  }catch(e){showToast('删除失败: '+e.message,'error');}
}

function _getTaskOutcome(t){
  if(!t.result||t.status==='pending'||t.status==='cancelled')return '';
  const r=t.result;
  const parts=[];
  // 关注
  if(r.followed!=null) parts.push(r.followed>0?`<span class="task-outcome ok">+${r.followed}关注</span>`:`<span class="task-outcome dim">0关注</span>`);
  // 私信发送
  const dmsSent=r.dms_sent??r.chatted??r.messaged;
  if(dmsSent!=null) parts.push(dmsSent>0?`<span class="task-outcome ok">+${dmsSent}私信</span>`:`<span class="task-outcome dim">0私信</span>`);
  // 新消息/收件箱
  const newMsg=r.new_messages??r.new_message_count??r.replied_count;
  if(newMsg!=null) parts.push(newMsg>0?`<span class="task-outcome hot">🔔${newMsg}新消息</span>`:`<span class="task-outcome dim">0新消息</span>`);
  // 升级/引流
  if(r.escalated!=null&&r.escalated>0) parts.push(`<span class="task-outcome escalate">⚡${r.escalated}升级</span>`);
  // 错误摘要
  if(t.status==='failed'&&r.error) parts.push(`<span class="task-outcome err" title="${r.error.substring(0,120)}">${r.error.substring(0,30)}…</span>`);
  return parts.join('');
}

/** isTrashList：回收站列表行（恢复/永久删除）；总览等用 taskRowForOverview */
function _taskRowInner(t,isTrashList){
  const bcls='badge-'+(t.status||'pending');
  const alias=t.device_label||ALIAS[t.device_id]||t.device_id?.substring(0,8)||'全部';
  const originTag=t.task_origin_label_zh?`<span style="font-size:9px;color:var(--text-muted);margin-left:4px">· ${t.task_origin_label_zh}</span>`:'';
  const tname=t.type_label_zh||TASK_NAMES[t.type]||t.type?.replace('tiktok_','').replace('telegram_','')||'未知';
  const trashMeta=isTrashList&&t.deleted_at?`<span style="font-size:9px;color:var(--text-muted);margin-left:6px">删于 ${new Date(t.deleted_at).toLocaleString('zh-CN')}</span>`:'';
  const tm=t.updated_at?new Date(t.updated_at).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
  const err=(t.status==='failed'&&t.result?.error)?t.result.error.substring(0,60):'';
  const stLabel={running:'运行中',completed:'已完成',failed:'失败',pending:'等待中',cancelled:'已取消'}[t.status]||t.status;
  let progressHtml='';
  if(t.status==='running'&&t.result?.progress!=null){
    const pct=t.result.progress;const pmsg=t.result.progress_msg||'';
    const stepDesc=_getStepDesc(t.type,pct)||pmsg;
    progressHtml=`<div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div><div class="progress-text">${pct}% ${stepDesc?'· '+stepDesc:''}</div>`;
  }
  const outcomeHtml=_getTaskOutcome(t);
  const errHtml=(t.status==='failed'&&err&&!outcomeHtml)?`<div style="color:#f87171;font-size:11px;margin-top:2px">${err}</div>`:'';
  const stuckHtml=(t.status==='pending'&&t.stuck_reason_zh)?`<div style="color:#f59e0b;font-size:11px;margin-top:2px" title="点击「详情」查看完整原因">⏸ ${t.stuck_reason_zh}</div>`:'';
  const wh=t.worker_host||t._worker||'';
  const workerHint=wh?`<span style="font-size:9px;color:var(--accent)" title="Worker IP">@${wh}</span>`:'';
  const tid=t.task_id||'';
  let actions='';
  let selCell='';
  if(isTrashList){
    actions=`<span style="display:inline-flex;align-items:center;gap:4px;flex-wrap:wrap"><button type="button" class="qa-btn" style="font-size:10px;padding:2px 8px" onclick="event.stopPropagation();showTaskDetail('${tid}')">详情</button><button type="button" class="qa-btn" title="恢复到任务列表" style="font-size:10px;padding:2px 6px;color:#22c55e;border-color:#22c55e44" onclick="event.stopPropagation();restoreTaskRecord('${tid}')">恢复</button><button type="button" class="qa-btn" title="从数据库永久删除" style="font-size:10px;padding:2px 6px;color:#f87171;border-color:#f8717144" onclick="event.stopPropagation();eraseTaskRecord('${tid}')">永久删除</button></span>`;
    selCell=`<input type="checkbox" aria-label="选择任务" style="accent-color:var(--accent);cursor:pointer" onclick="event.stopPropagation()" onchange="taskBulkToggle('${tid}',this.checked)" ${_taskBulkSelected.has(tid)?'checked':''} />`;
  }else{
    const delBtn=_taskRecordDeletable(t.status)
      ?`<button type="button" class="qa-btn" title="移入回收站（可在回收站恢复或永久删除）" style="font-size:10px;padding:2px 6px;color:#f87171;border-color:#f8717144" onclick="event.stopPropagation();deleteTaskRecord('${tid}')">移入回收站</button>`
      :'';
    actions=`<span style="display:inline-flex;align-items:center;gap:4px;flex-wrap:wrap"><button type="button" class="qa-btn" style="font-size:10px;padding:2px 8px" onclick="event.stopPropagation();showTaskDetail('${tid}')">详情</button>${delBtn}</span>`;
    selCell=_taskRecordDeletable(t.status)
      ?`<input type="checkbox" aria-label="选择任务" style="accent-color:var(--accent);cursor:pointer" onclick="event.stopPropagation()" onchange="taskBulkToggle('${tid}',this.checked)" ${_taskBulkSelected.has(tid)?'checked':''} />`
      :'<span style="display:inline-block;width:14px" title="运行中/等待中不可删"></span>';
  }
  return `<tr style="cursor:pointer" onclick="showTaskDetail('${tid}')"><td style="text-align:center;width:40px;vertical-align:middle" onclick="event.stopPropagation()">${selCell}</td><td><b>${tname}</b>${trashMeta}${originTag}${outcomeHtml?'<div style="margin-top:3px;display:flex;gap:4px;flex-wrap:wrap">'+outcomeHtml+'</div>':''}${progressHtml}${errHtml}${stuckHtml}</td><td>${alias} ${workerHint}${t.device_id&&!t.device_label?' '+_workerBadgeById(t.device_id):''}</td><td><span class="badge ${bcls}">${stLabel}</span></td><td style="color:var(--text-muted)">${tm}</td><td>${actions}</td></tr>`;
}

function taskRow(t){
  return _taskRowInner(t, currentFilter==='trash');
}

function taskRowForOverview(t){
  return _taskRowInner(t,false);
}

async function cancelTask(taskId){
  if(!confirm('确定取消此任务？'))return;
  try{await api('POST','/tasks/'+taskId+'/cancel');showToast('任务已取消','success');loadTasks();}catch(e){showToast('取消失败: '+e.message,'error');}
}

async function cancelAllTasks(){
  if(!confirm('确定取消所有运行中的任务？'))return;
  try{const r=await api('POST','/tasks/cancel-all');showToast('已取消 '+(r.cancelled||0)+' 个任务','success');loadTasks();}catch(e){showToast('取消失败','error');}
}

let _taskPollTimer=null;
function _getTaskPollInterval(){
  // Use longer interval when WS is connected (WS handles real-time updates)
  if(typeof _unifiedWs!=='undefined'&&_unifiedWs&&_unifiedWs.readyState===WebSocket.OPEN) return 15000;
  return 5000; // faster fallback when WS disconnected
}
function _startTaskPoll(){
  if(_taskPollTimer)return;
  function _doPoll(){
    _taskPollTimer=setTimeout(async()=>{
      await loadTasks();
      const hasActive=allTasks.some(t=>t.status==='running'||t.status==='pending');
      if(hasActive) _doPoll(); // schedule next poll with fresh interval
      else _taskPollTimer=null;
    },_getTaskPollInterval());
  }
  _doPoll();
}

/** 回收站数量角标（GET /tasks/count?trash_only=true） */
async function _refreshTrashCountBadge(){
  try{
    const r=await api('GET','/tasks/count?trash_only=true&_ts='+Date.now());
    const raw=r&&r.count!=null?r.count:0;
    const n=typeof raw==='number'?raw:parseInt(String(raw),10)||0;
    const el=document.getElementById('tf-trash-badge');
    if(el) el.textContent=n>0?' ('+n+')':'';
  }catch(_e){}
}

function _flashOcTaskListCard(){
  const el=document.getElementById('oc-task-list-card');
  if(!el)return;
  el.classList.remove('oc-task-list-flash');
  void el.offsetWidth;
  el.classList.add('oc-task-list-flash');
  setTimeout(function(){el.classList.remove('oc-task-list-flash');},850);
}

function _applyTaskCountHint(shown,total){
  const countEl=document.getElementById('task-count-info');
  const help=document.getElementById('task-count-help-btn');
  const cf=typeof currentFilter==='undefined'?'all':currentFilter;
  const lab=_TASK_FILTER_LABEL_ZH[cf]||cf;
  const bits=['「显示 '+shown+' / '+total+'」：前者为本页已展示条数（默认每页50，可点「加载更多」），后者为当前筛选下任务总数。'];
  if(cf==='trash') bits.push('回收站仅本机已软删记录。');
  else if(cf!=='all') bits.push('当前为「'+lab+'」视图。');
  else bits.push('「全部」合并本机与在线 Worker，总数可能随同步波动。');
  const txt=bits.join('');
  if(countEl) countEl.setAttribute('title',txt);
  if(help) help.setAttribute('title',txt);
}

async function _syncErrorAnalysisForTaskCenter(trash,cf){
  if(trash||typeof _loadErrorAnalysis!=='function')return;
  if(cf==='failed'){
    await _loadErrorAnalysis();
    const panel=document.getElementById('error-analysis-panel');
    if(panel&&panel.style.display!=='none'&&_eapAutoExpandPending){
      if(!_eapOpen){
        _eapOpen=true;
        const detail=document.getElementById('eap-detail');
        const chev=document.getElementById('eap-chevron');
        if(detail) detail.style.display='';
        if(chev) chev.style.transform='rotate(180deg)';
      }
    }
    _eapAutoExpandPending=false;
    return;
  }
  const ep=document.getElementById('error-analysis-panel');
  if(ep&&ep.style.display!=='none'){
    await _loadErrorAnalysis();
  }
}

async function loadTasks(){
  try{
    const trash=typeof currentFilter!=='undefined'&&currentFilter==='trash';
    const cf=typeof currentFilter!=='undefined'?currentFilter:'all';
    let raw;
    if(trash){
      raw=await api('GET','/tasks?trash_only=true&limit='+_TASK_LIST_LIMIT_STATUS);
    }else if(_TASK_STATUS_TABS.indexOf(cf)>=0){
      raw=await api('GET','/tasks?limit='+_TASK_LIST_LIMIT_STATUS+'&status='+encodeURIComponent(cf));
    }else{
      raw=await api('GET','/tasks?limit='+_TASK_LIST_LIMIT_ALL);
    }
    // 统一排序：运行中/等待中优先，同状态按 updated_at 降序（最新在前）
    allTasks=raw.sort((a,b)=>{
      const rank={running:0,pending:1,completed:2,failed:2,cancelled:3};
      const ra=rank[a.status]??2, rb=rank[b.status]??2;
      if(ra!==rb) return ra-rb;
      return (b.updated_at||'').localeCompare(a.updated_at||'');
    });
    _taskBulkSelected.forEach(function(id){
      const t=allTasks.find(function(x){return x.task_id===id;});
      if(!t) _taskBulkSelected.delete(id);
      else if(!trash&&!_taskRecordDeletable(t.status)) _taskBulkSelected.delete(id);
    });
    renderTasks();
    _updateOpsStatusBar();
    const ovBody=document.getElementById('ov-tasks-body');
    if(ovBody){
      let rowsForOv=allTasks.slice(0,8);
      if(trash||cf!=='all'){
        try{
          const norm=await api('GET','/tasks?limit=8');
          rowsForOv=norm;
        }catch(_e){rowsForOv=[];}
      }
      ovBody.innerHTML=rowsForOv.map(taskRowForOverview).join('');
      if(!rowsForOv.length)ovBody.innerHTML='<tr><td colspan="6" style="text-align:center;color:var(--text-muted);padding:20px">暂无任务</td></tr>';
    }
    // 有运行中任务时自动轮询（回收站视图不依赖轮询）
    const hasActive=!trash&&allTasks.some(t=>t.status==='running'||t.status==='pending');
    if(hasActive)_startTaskPoll();
    // 同步设备卡任务状态条（P6）
    if(typeof _syncTaskMap==='function') _syncTaskMap();
    await _syncErrorAnalysisForTaskCenter(trash,cf);
  }catch(e){
    if(typeof showToast==='function'){
      showToast('任务列表加载失败: '+(e&&e.message?e.message:String(e)),'error');
    }
  }
  try{await _refreshTrashCountBadge();}catch(_e){}
}

let _taskDisplayLimit=50;

function renderTasks(){
  const filtered=_tasksFilteredForView();
  const tbody=document.getElementById('task-tbody');const empty=document.getElementById('task-empty');
  if(!filtered.length){tbody.innerHTML='';empty.style.display='block';
    const countEl=document.getElementById('task-count-info');
    if(countEl){
      const suffix=currentFilter==='trash'?'（回收站）':'';
      countEl.textContent=`显示 0 / 0 条${suffix}`;
      _applyTaskCountHint(0,0);
    }
    const moreBtn=document.getElementById('task-load-more');
    if(moreBtn) moreBtn.style.display='none';
    const emptyHint=empty.querySelector('.task-empty-hint');
    if(emptyHint){
      if(currentFilter==='trash') emptyHint.textContent='回收站中没有任务';
      else if(currentFilter==='failed') emptyHint.textContent='暂无失败任务；若有新失败将出现在此列表';
      else emptyHint.textContent='从总览页或 TikTok 面板创建你的第一个任务';
    }
    _updateBulkDeleteBtn();
    _syncTaskSelectAllCheckbox();
    return;}
  empty.style.display='none';
  tbody.innerHTML=filtered.slice(0,_taskDisplayLimit).map(taskRow).join('');
  // 更新计数和加载更多
  const countEl=document.getElementById('task-count-info');
  if(countEl){
    const suffix=currentFilter==='trash'?'（回收站）':'';
    const shown=Math.min(filtered.length,_taskDisplayLimit);
    countEl.textContent=`显示 ${shown} / ${filtered.length} 条${suffix}`;
    _applyTaskCountHint(shown,filtered.length);
  }
  const moreBtn=document.getElementById('task-load-more');
  if(moreBtn) moreBtn.style.display=filtered.length>_taskDisplayLimit?'inline-block':'none';
  _updateBulkDeleteBtn();
  _syncTaskSelectAllCheckbox();
}

function loadMoreTasks(){
  _taskDisplayLimit+=50;
  renderTasks();
}

function filterTasks(f){
  currentFilter=f;
  if(f==='failed') _eapAutoExpandPending=true;
  _taskBulkSelected.clear();
  document.querySelectorAll('[id^="tf-"]').forEach(b=>b.style.borderColor='var(--border)');
  const btn=document.getElementById('tf-'+f);
  if(btn) btn.style.borderColor='var(--accent)';
  loadTasks().catch(function(err){
    if(typeof showToast==='function'){
      showToast('加载失败: '+(err&&err.message?err.message:String(err)),'error');
    }
  });
}

/* ── Chat ── */
function fillCmd(cmd){document.getElementById('chat-input').value=cmd;document.getElementById('chat-input').focus();}

/* ★ P2-6: 填充输入框并立即发送（用于快捷指令卡片） */
function fillAndSend(cmd){
  const inp=document.getElementById('chat-input');
  if(!inp) return;
  inp.value=cmd;
  if(typeof sendChat==='function') sendChat();
}

function quickCmd(cmd){
  navigateToPage('chat');
  document.getElementById('chat-input').value=cmd;
  setTimeout(sendChat,200);
}

function quickCmdDev(dev,action){quickCmd(dev+action);}

/* ── 快速指令 ── */
function _aiQuickSet(cmd){
  document.getElementById('ai-quick-input').value=cmd;
  document.getElementById('ai-quick-input').focus();
}
async function _aiQuickExec(){
  const input=document.getElementById('ai-quick-input');
  const cmd=input.value.trim();
  if(!cmd){showToast('请输入指令');return;}
  const result=document.getElementById('ai-quick-result');
  result.style.display='block';
  result.innerHTML='<span style="color:var(--accent)">&#9203; 执行中...</span>';
  try{
    const d=await api('POST','/ai/quick-command',{command:cmd});
    if(d.ok){
      result.innerHTML=`<span style="color:#22c55e">&#9989; ${d.message}</span>`;
      showToast(d.message,'success');
      setTimeout(()=>{loadTasks().then(()=>_startTaskPoll());},1500);
    }else{
      result.innerHTML=`<span style="color:var(--yellow)">&#9888; ${d.message}</span>${d.hint?'<div style="color:var(--text-dim);margin-top:4px;font-size:11px">'+d.hint+'</div>':''}`;
    }
  }catch(e){
    result.innerHTML=`<span style="color:var(--red)">&#10060; ${e.message}</span>`;
  }
}

/* ── 智能故障诊断 ── */
async function diagnoseDev(deviceId){
  showToast('正在诊断 '+deviceId.substring(0,8)+'...');
  try{
    const r=await api('GET',`/devices/${deviceId}/diagnose`);
    const issues=(r.issues||[]).join('\n');
    const fixes=(r.fixes||[]).map(f=>f.label).join(', ');
    const checks=r.checks||{};
    let msg=`诊断结果:\n${issues}`;
    if(fixes) msg+=`\n\n建议修复: ${fixes}`;
    if(checks.battery!=null) msg+=`\n电量: ${checks.battery}%`;
    alert(msg);
    if(r.fixes&&r.fixes.length){
      if(confirm('是否自动执行修复操作？')){
        for(const fix of r.fixes){
          showToast('执行: '+fix.label+'...');
          try{await api('POST',`/devices/${deviceId}/fix`,{action:fix.action});}catch(e){}
        }
        showToast('修复操作已执行，请等待30秒后查看');
        setTimeout(()=>{loadDevices();loadHealth();},15000);
      }
    }
  }catch(e){showToast('诊断失败: '+e.message,'warn');}
}

async function fixDev(deviceId,action){
  showToast('正在修复...');
  try{
    const r=await api('POST',`/devices/${deviceId}/fix`,{action:action});
    showToast(r.output||'修复操作已执行');
    setTimeout(()=>{loadDevices();loadHealth();},10000);
  }catch(e){showToast('修复失败: '+e.message,'warn');}
}

/* ── 任务详情：门禁/失败说明（与后端 gate_evaluation 一致） ── */
function _taskDetailGeoGlossary(params, ge){
  const hasGeoParam = params && Object.prototype.hasOwnProperty.call(params, 'geo_filter');
  const hasGate = ge && typeof ge === 'object';
  if (!hasGeoParam && !hasGate) return '';
  return `<div class="detail-row"><span class="detail-label">参数与门禁</span><div style="flex:1;font-size:11px;color:var(--text-dim);line-height:1.5;padding:8px 10px;background:rgba(99,102,241,.07);border-radius:8px;border:1px solid rgba(99,102,241,.22)">
    <div>任务参数里的 <code style="font-size:10px">geo_filter</code> 表示<strong>任务侧</strong>是否做地理筛选；门禁摘要里的 <strong>GEO 开</strong> / <code style="font-size:10px">geo_enforced</code> 表示<strong>策略层</strong>是否校验出口 IP 国家。二者含义不同，可同时出现。</div>
    ${hasGate ? `<div style="margin-top:6px;font-size:10px;color:var(--text-muted)">当前门禁：等级 ${ge.tier||'—'} · 模式 ${ge.gate_mode||'—'} · 预检 ${ge.preflight_mode||'—'}</div>` : ''}
  </div></div>`;
}
function _gateHintFromCode(ge){
  if (!ge || (!ge.hint_code && !(ge.hint_message && String(ge.hint_message).trim()))) return '';
  const tipServer = (ge.hint_message || '').trim();
  /* 主控 GET /tasks 已回填 hint_message；仅浏览器旧缓存无文案时提示刷新 */
  let tip = tipServer;
  if (!tip && ge.hint_code) {
    tip = '（请重新打开任务详情或刷新列表以加载完整指引。）';
  }
  const codeLine = ge.hint_code ? `<code style="font-size:10px;color:var(--text-muted)">${ge.hint_code}</code><br>` : '';
  return `<div class="detail-row"><span class="detail-label">指引</span><div style="flex:1;font-size:11px;color:#fbbf24;line-height:1.5">${codeLine}${tip}</div></div>`;
}
function _taskDetailFailureHints(err, ge){
  const rows = [];
  const gh = _gateHintFromCode(ge);
  if (gh) rows.push(gh);
  /* 后端已回填 hint_message 时不再追加与错误字符串关键字重复的兜底条，避免「网络失败却先写 VPN」 */
  if (ge && String(ge.hint_message || '').trim()) {
    return rows.join('');
  }
  const e = (err || '').toLowerCase();
  if (!err && !rows.length) return '';
  const parts = [];
  if (e.includes('public ip') || (e.includes('geo') && e.includes('失败'))){
    parts.push('• <b>出口 IP / GEO</b>：确认 VPN 已连接且节点国家与任务目标一致；可在「VPN 管理」重连。');
  }
  if (e.includes('network') || e.includes('外网') || e.includes('curl')){
    parts.push('• <b>网络</b>：确认手机 Wi‑Fi/蜂窝可用；必要时在设备页「诊断」。');
  }
  if (e.includes('vpn')){
    parts.push('• <b>VPN</b>：打开 v2rayNG 并连接；预检未通过时任务不会执行。');
  }
  if (parts.length) {
    rows.push(`<div class="detail-row"><span class="detail-label">处理建议</span><div style="flex:1;font-size:11px;color:#fbbf24;line-height:1.5">${parts.join('<br>')}</div></div>`);
  }
  return rows.join('');
}

/* ── P0-4: FB 养号档案卡（browse_feed 专用结构化渲染）── */
function _renderFbWarmupCard(r){
  const scrolls=r.scrolls||0;
  const target=r.target_scrolls||0;
  const likes=r.likes||0;
  const videos=r.video_dwells||0;
  const pulls=r.pull_refreshes||0;
  const minutes=r.minutes_equivalent||0;
  const likeRate=(r.like_rate_actual!=null)?(r.like_rate_actual*100).toFixed(1)+'%':'—';
  const fallback=r.home_tab_fallback||'';
  const progressPct=target>0?Math.min(100,Math.round(scrolls/target*100)):0;
  const fallbackBadge=(fallback && fallback!=='smart_tap')
    ? `<span style="display:inline-block;margin-left:6px;padding:1px 6px;border-radius:4px;background:rgba(234,179,8,.18);color:#eab308;font-size:9px">Home回退: ${fallback}</span>`
    : '';

  // 失败（home_tab_not_found 等）：强调 hint
  if(r.error_code){
    return `<div class="detail-row" style="background:rgba(239,68,68,.08);border-radius:10px;padding:12px;border:1px solid rgba(239,68,68,.35)">
      <span class="detail-label">&#128310; 养号档案</span>
      <div style="flex:1;font-size:12px;line-height:1.6">
        <div><b style="color:#ef4444">脚本中止</b> <code style="font-size:10px;color:#f87171">${r.error_code}</code></div>
        <div style="margin-top:6px;color:var(--text-dim);font-size:11px">${r.error_hint||'无额外修复建议'}</div>
      </div>
    </div>`;
  }

  // 风控中断高亮
  const riskBadge=r.risk_detected
    ? `<div style="margin-top:8px;padding:6px 10px;background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.35);border-radius:6px;color:#ef4444;font-size:11px">&#9888; 风控中断: ${r.risk_detected}</div>`
    : '';

  // P1-2: phase 标签 + 迁移提示
  const phaseColors={cold_start:'#60a5fa',growth:'#22c55e',mature:'#8b5cf6',cooldown:'#ef4444'};
  const phaseLabels={cold_start:'冷启动',growth:'成长期',mature:'成熟号',cooldown:'冷却中'};
  const phaseBadge=r.phase?`<span style="display:inline-block;margin-left:6px;padding:1px 7px;border-radius:4px;background:${phaseColors[r.phase]||'#64748b'}22;color:${phaseColors[r.phase]||'#64748b'};font-size:9px;font-weight:600">${phaseLabels[r.phase]||r.phase}</span>`:'';
  const pt=r.phase_transition;
  const transitionBadge=(pt&&pt.changed)
    ?`<div style="margin-top:6px;padding:5px 9px;background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.32);border-radius:6px;color:#22c55e;font-size:10px">&#127919; phase 迁移: <b>${phaseLabels[pt.from]||pt.from}</b> &rarr; <b>${phaseLabels[pt.to]||pt.to}</b> (${pt.reason||''})</div>`
    :'';
  const cfgSrcBadge=r.config_source==='playbook'
    ?`<span title="节奏参数来自 config/facebook_playbook.yaml（热加载）" style="display:inline-block;margin-left:4px;padding:1px 5px;border-radius:3px;background:rgba(139,92,246,.14);color:#a78bfa;font-size:9px">playbook</span>`
    :'';

  return `<div class="detail-row" style="background:linear-gradient(135deg,rgba(24,119,242,.08),rgba(139,92,246,.05));border-radius:10px;padding:12px;border:1px solid rgba(24,119,242,.28)">
    <span class="detail-label">&#128240; 养号档案</span>
    <div style="flex:1">
      <div style="font-size:12px;color:var(--text);line-height:1.45">${r.narrative||''}${phaseBadge}${cfgSrcBadge}${fallbackBadge}</div>
      ${transitionBadge}
      <div style="margin-top:8px;display:grid;grid-template-columns:repeat(4,1fr);gap:8px;font-size:11px">
        <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:6px;padding:6px 8px">
          <div style="color:var(--text-dim);font-size:9px">滑动进度</div>
          <div style="font-weight:600">${scrolls}${target>0?` / ${target}`:''} <span style="color:var(--text-dim);font-size:10px">(${progressPct}%)</span></div>
        </div>
        <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:6px;padding:6px 8px">
          <div style="color:var(--text-dim);font-size:9px">点赞次数</div>
          <div style="font-weight:600">${likes} <span style="color:var(--text-dim);font-size:10px">(${likeRate})</span></div>
        </div>
        <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:6px;padding:6px 8px">
          <div style="color:var(--text-dim);font-size:9px">看视频</div>
          <div style="font-weight:600">${videos} <span style="color:var(--text-dim);font-size:10px">次</span></div>
        </div>
        <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:6px;padding:6px 8px">
          <div style="color:var(--text-dim);font-size:9px">下拉刷新</div>
          <div style="font-weight:600">${pulls}</div>
        </div>
      </div>
      <div style="margin-top:6px;color:var(--text-muted);font-size:10px">等效真人: 约 <b>${minutes}</b> 分钟 · 平均 ${r.dwell_seconds_total?(r.dwell_seconds_total/Math.max(1,scrolls)).toFixed(1):'—'} 秒/屏</div>
      ${riskBadge}
    </div>
  </div>`;
}

/* P2-4 Sprint B: Profile Hunt 结果卡片 */
function _renderFbProfileHuntCard(r){
  const total=r.candidates_total||0;
  const processed=r.processed||0;
  const l1=r.l1_pass||0;
  const l2=r.l2_run||0;
  const matched=r.matched||0;
  const actioned=r.actioned||0;
  const sk=r.skipped||{};
  const personaName=r.persona_name||r.persona_key||'';
  const action=r.action_on_match||'none';
  const actionLabels={none:'仅识别',follow:'自动关注',add_friend:'加好友'};
  const hitRate=processed>0?((matched/processed)*100).toFixed(1):'0';
  const l1Rate=processed>0?((l1/processed)*100).toFixed(1):'0';
  const l2HitRate=l2>0?((matched/l2)*100).toFixed(1):'—';
  const riskBadge=r.risk_interrupted
    ? `<div style="margin-top:8px;padding:6px 10px;background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.35);border-radius:6px;color:#ef4444;font-size:11px">&#9888; 风控中断: ${r.risk_interrupted}</div>`
    : '';
  const results=(r.results||[]).slice(0,20);
  const rowsHtml=results.map(it=>{
    const icon=it.match?'&#9989;':(it.reason==='l1_below_threshold'?'&#9898;':(it.reason?'&#10060;':'&#9898;'));
    const color=it.match?'#22c55e':(it.reason?'#ef4444':'var(--text-muted)');
    const scoreStr=typeof it.score==='number'?it.score.toFixed(0):it.score;
    const stage=it.stage||'-';
    const note=it.match?(it.action_ok?`<span style="color:#22c55e">${actionLabels[action]||action}&nbsp;&#10004;</span>`:`<span style="color:#f59e0b">动作失败</span>`)
      :(it.reason?`<span style="color:var(--text-dim);font-size:10px">${it.reason}</span>`:'');
    return `<div style="display:grid;grid-template-columns:18px 1fr 50px 40px 1fr;gap:6px;padding:4px 0;border-bottom:1px dashed var(--border);font-size:11px;align-items:center">
      <span style="color:${color}">${icon}</span>
      <span style="color:var(--text)" title="${it.name||''}">${it.name||'—'}</span>
      <span style="color:var(--text-dim)">${stage}</span>
      <span style="color:var(--text-dim);text-align:right">${scoreStr}</span>
      <span>${note}</span>
    </div>`;
  }).join('');
  const moreNote=(r.results||[]).length>20?`<div style="font-size:10px;color:var(--text-muted);margin-top:4px;text-align:center">…共 ${r.results.length} 条，仅显示前 20</div>`:'';

  return `<div class="detail-row" style="background:linear-gradient(135deg,rgba(139,92,246,.10),rgba(24,119,242,.05));border-radius:10px;padding:12px;border:1px solid rgba(139,92,246,.32)">
    <span class="detail-label">&#127919; 画像识别</span>
    <div style="flex:1">
      <div style="font-size:12px;color:var(--text);line-height:1.45">
        <b>${personaName}</b> · 动作: <span style="color:#a78bfa">${actionLabels[action]||action}</span>
      </div>
      <div style="margin-top:8px;display:grid;grid-template-columns:repeat(4,1fr);gap:8px;font-size:11px">
        <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:6px;padding:6px 8px">
          <div style="color:var(--text-dim);font-size:9px">处理</div>
          <div style="font-weight:600">${processed} / ${total}</div>
        </div>
        <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:6px;padding:6px 8px">
          <div style="color:var(--text-dim);font-size:9px">L1 通过</div>
          <div style="font-weight:600">${l1} <span style="color:var(--text-dim);font-size:10px">(${l1Rate}%)</span></div>
        </div>
        <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:6px;padding:6px 8px">
          <div style="color:var(--text-dim);font-size:9px">L2 深判</div>
          <div style="font-weight:600">${l2}</div>
        </div>
        <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:6px;padding:6px 8px">
          <div style="color:var(--text-dim);font-size:9px">命中</div>
          <div style="font-weight:600;color:#22c55e">${matched} <span style="color:var(--text-dim);font-size:10px">(${hitRate}%)</span></div>
        </div>
      </div>
      <div style="margin-top:6px;color:var(--text-muted);font-size:10px">
        L2 命中率: ${l2HitRate}${typeof l2HitRate==='string'&&l2HitRate!=='—'?'%':''}  ·  动作执行: ${actioned}  ·  跳过: L1失败 ${sk.l1_fail||0} / 配额满 ${sk.l2_cap||0} / 去重 ${sk.cached||0} / 搜索失败 ${sk.search_fail||0}${sk.risk_pause?` / L2 风控停 ${sk.risk_pause}`:''}
      </div>
      ${riskBadge}
      ${results.length?`<div style="margin-top:10px;background:var(--bg-main);border:1px solid var(--border);border-radius:6px;padding:8px 10px">
        <div style="font-size:10px;color:var(--text-dim);margin-bottom:4px">识别明细（前 20）</div>
        ${rowsHtml}
        ${moreNote}
      </div>`:''}
    </div>
  </div>`;
}

/* ── 任务详情 Modal ── */
async function showTaskDetail(taskId){
  const incTrash=typeof currentFilter!=='undefined'&&currentFilter==='trash';
  let t=allTasks.find(x=>x.task_id===taskId);
  try{
    const q=incTrash?'?include_deleted=true':'';
    const fresh=await api('GET','/tasks/'+encodeURIComponent(taskId)+q);
    if(fresh&&fresh.task_id) t=fresh;
  }catch(e){
    if(!t){showToast('任务未找到','error');return;}
  }
  if(!t){showToast('任务未找到','error');return;}
  const alias=t.device_label||ALIAS[t.device_id]||t.device_id?.substring(0,8)||'未知';
  const tname=t.type_label_zh||TASK_NAMES[t.type]||t.type||'未知';
  const stLabel={running:'🟢 运行中',completed:'✅ 已完成',failed:'❌ 失败',pending:'⏳ 等待中',cancelled:'🚫 已取消'}[t.status]||t.status;
  const tm=t.created_at?new Date(t.created_at).toLocaleString('zh-CN'):'—';
  const upd=t.updated_at?new Date(t.updated_at).toLocaleString('zh-CN'):'—';
  const elapsed=t.created_at?Math.round((Date.now()-(new Date(t.created_at).getTime()))/1000):0;
  const elapsedStr=elapsed>3600?`${Math.floor(elapsed/3600)}h ${Math.floor((elapsed%3600)/60)}m`:(elapsed>60?`${Math.floor(elapsed/60)}m ${elapsed%60}s`:`${elapsed}s`);
  const params=t.params||{};
  const result=t.result||{};
  const err=(result.error||'').substring(0,200);
  const workerIp=t.worker_host||t._worker||'';
  const worker=workerIp?`<div class="detail-row"><span class="detail-label">Worker节点</span><span style="color:var(--accent)" title="执行该任务记录的节点（集群时可能为 Worker IP）">${workerIp}</span></div>`:'';
  const originRow=t.task_origin_label_zh?`<div class="detail-row"><span class="detail-label">来源</span><span style="font-size:12px">${t.task_origin_label_zh}${t.task_origin?` <code style="font-size:9px;color:var(--text-muted)">${t.task_origin}</code>`:''}</span></div>`:'';
  const phaseRow=t.phase_caption?`<div class="detail-row"><span class="detail-label">phase 说明</span><span style="font-size:11px;color:var(--text-dim);line-height:1.45">${t.phase_caption}</span></div>`:'';
  const polRow=t.execution_policy_hint?`<div class="detail-row"><span class="detail-label">执行策略</span><span style="font-size:10px;color:var(--text-muted);line-height:1.45">${t.execution_policy_hint}</span></div>`:'';
  const ge=result.gate_evaluation;
  const geoGlossary=_taskDetailGeoGlossary(params,ge);
  const failHints=_taskDetailFailureHints(err, ge);
  const gateBlock=ge&&typeof ge==='object'?`<div class="detail-row"><span class="detail-label">门禁详情</span><div style="flex:1;font-size:10px;color:var(--text-dim)">
    <div><b>等级</b> ${ge.tier||'—'} · <b>模式</b> ${ge.gate_mode||'—'} · <b>预检</b> ${ge.preflight_mode||'—'} · <b>GEO</b> ${ge.geo_enforced===false?'关':'开'}</div>
    <pre style="margin:6px 0 0;white-space:pre-wrap;max-height:120px;overflow:auto;background:var(--bg-main);padding:6px;border-radius:6px;border:1px solid var(--border)">${JSON.stringify(ge.connectivity||{},null,2).substring(0,1200)}</pre>
  </div></div>`:'';

  let resultHtml='';
  if(t.status==='running'&&result.progress!=null){
    resultHtml=`<div class="detail-row"><span class="detail-label">进度</span><div style="flex:1"><div class="progress-bar" style="margin:0"><div class="progress-fill" style="width:${result.progress}%"></div></div><div style="font-size:11px;color:var(--text-dim);margin-top:2px">${result.progress}% ${result.progress_msg||''}</div></div></div>`;
  } else if(result.card_type==='fb_warmup'){
    // P0-4: FB 养号档案卡 — 结构化渲染 browse_feed 任务输出
    resultHtml=_renderFbWarmupCard(result);
  } else if(result.card_type==='fb_profile_hunt'){
    // P2-4 Sprint B: 目标画像 Profile Hunt 卡片
    resultHtml=_renderFbProfileHuntCard(result);
  } else if(Object.keys(result).length){
    const rKeys=Object.entries(result).filter(([k])=>!['success','error','screenshot_path','progress','progress_msg','gate_evaluation','card_type'].includes(k));
    if(rKeys.length) resultHtml=`<div class="detail-row"><span class="detail-label">结果摘要</span><pre style="font-size:10px;color:var(--text-dim);white-space:pre-wrap;margin:0;flex:1">${JSON.stringify(Object.fromEntries(rKeys),null,2).substring(0,400)}</pre></div>`;
  }

  // Gate hint: fb_risk_cooldown 给个人类可读提示
  if(ge && ge.connectivity && ge.connectivity.fb_risk_cooldown){
    const c=ge.connectivity.fb_risk_cooldown;
    resultHtml = `<div class="detail-row" style="background:rgba(239,68,68,.08);border-radius:8px;padding:10px;margin:8px 0;border:1px solid rgba(239,68,68,.35)">
      <span class="detail-label">&#128308; 风控冷却</span>
      <div style="flex:1;font-size:11px;line-height:1.6">
        <div>该设备最近 <b>${c.window_hours}h</b> 内累计 <b style="color:#ef4444">${c.recent_count}</b> 次 FB 风控事件（阈值 ${c.threshold}）。</div>
        <div style="color:var(--text-dim);margin-top:4px">建议: 暂停该账号 24h、检查 VPN 出口、或切换到 cold_start 养号预设。等待窗口滑出后可自动恢复。</div>
      </div>
    </div>` + resultHtml;
  }

  const canCancel=t.status==='running'||t.status==='pending';
  const inTrash=!!(t.deleted_at);
  const canDelete=!canCancel&&!inTrash;
  const canRestore=inTrash;
  const canErase=inTrash;
  const canRetry=t.status==='failed'&&!inTrash;
  // Sprint C-1: extract_members 任务完成后可以"一键串链"到 profile_hunt
  const memberCount=(result&&result.members&&result.members.length)||0;
  const canHunt=(t.type==='facebook_extract_members')&&(t.status==='completed')&&(!inTrash)&&(memberCount>0);

  const modal=document.createElement('div');
  modal.id='task-detail-modal';
  modal.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
  modal.innerHTML=`
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:16px;padding:24px;width:min(520px,95vw);max-height:80vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,0.4)">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px">
        <div>
          <div style="font-size:16px;font-weight:700;color:var(--text)">${tname}</div>
          <div style="font-size:11px;color:var(--text-dim);margin-top:2px;font-family:monospace">${taskId}</div>
        </div>
        <button onclick="document.getElementById('task-detail-modal').remove()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:18px;padding:4px">✕</button>
      </div>
      <style>
        .detail-row{display:flex;align-items:flex-start;gap:12px;padding:8px 0;border-bottom:1px solid var(--border)}
        .detail-row:last-child{border-bottom:none}
        .detail-label{font-size:11px;color:var(--text-dim);width:80px;flex-shrink:0;padding-top:2px}
      </style>
      <div class="detail-row"><span class="detail-label">状态</span><span>${stLabel}</span></div>
      <div class="detail-row"><span class="detail-label">设备</span><div style="flex:1"><div style="color:var(--accent)">${alias}</div><div style="font-size:10px;color:var(--text-dim);font-family:monospace;margin-top:2px" title="ADB 序列号（与设备管理一致）">${t.device_id||'—'}</div></div></div>
      ${originRow}
      ${phaseRow}
      ${polRow}
      ${worker}
      <div class="detail-row"><span class="detail-label">创建时间</span><span>${tm}</span></div>
      <div class="detail-row"><span class="detail-label">更新时间</span><span>${upd}</span></div>
      <div class="detail-row"><span class="detail-label">已运行</span><span>${elapsedStr}</span></div>
      ${Object.keys(_taskParamsDisplay(params)).length?`<div class="detail-row"><span class="detail-label">参数</span><pre style="font-size:10px;color:var(--text-dim);white-space:pre-wrap;margin:0;flex:1">${JSON.stringify(_taskParamsDisplay(params),null,2)}</pre></div>`:''}
      ${geoGlossary}
      ${err?`<div class="detail-row"><span class="detail-label">错误</span><span style="color:#f87171;font-size:12px">${err}</span></div>`:''}
      ${failHints}
      ${gateBlock}
      ${resultHtml}
      <div style="display:flex;gap:8px;margin-top:20px;flex-wrap:wrap">
        ${canHunt?`<button class="qa-btn" style="color:#a78bfa;border-color:#8b5cf6;background:rgba(139,92,246,.15);font-weight:600" onclick="_tdLaunchHunt('${taskId}','${t.device_id||''}',${memberCount})">🧠 用这批 ${memberCount} 人做画像识别</button>`:''}
        ${canCancel?`<button class="qa-btn" style="color:var(--yellow);border-color:var(--yellow)" onclick="_tdAction('cancel','${taskId}')">⏹ 取消任务</button>`:''}
        ${canRetry?`<button class="qa-btn" style="color:var(--accent);border-color:var(--accent)" onclick="_tdAction('retry','${taskId}')">🔄 重新提交</button>`:''}
        ${canRestore?`<button class="qa-btn" style="color:#22c55e;border-color:#22c55e" onclick="_tdAction('restore','${taskId}')">♻ 恢复任务</button>`:''}
        ${canErase?`<button class="qa-btn" style="color:#f87171;border-color:#f87171" onclick="_tdAction('erase','${taskId}')">永久删除</button>`:''}
        ${canDelete?`<button class="qa-btn" style="color:#f87171;border-color:#f87171" onclick="_tdAction('delete','${taskId}')">🗑 移入回收站</button>`:''}
        <button class="qa-btn" style="margin-left:auto" onclick="document.getElementById('task-detail-modal').remove()">关闭</button>
      </div>
    </div>`;
  document.getElementById('task-detail-modal')?.remove();
  document.body.appendChild(modal);
  modal.addEventListener('click',e=>{if(e.target===modal)modal.remove();});
}

async function purgeTasks(days){
  const d=days||7;
  if(!confirm(`确定删除 ${d} 天前的已完成/失败任务记录？`))return;
  try{
    const r=await api('POST',`/tasks/purge?days=${d}`);
    showToast(`已清理 ${r.deleted||0} 条旧任务`,'success');
    _taskDisplayLimit=50;
    await loadTasks();
  }catch(e){showToast('清理失败: '+e.message,'error');}
}

/** 移入回收站（列表/详情）；与后端 DELETE 一致：运行中/等待中不可删 */
async function deleteTaskRecord(taskId){
  if(!taskId)return;
  if(!confirm('确定将这条任务移入回收站？可在「回收站」恢复或永久删除。'))return;
  try{
    await api('DELETE','/tasks/'+encodeURIComponent(taskId));
    _taskBulkSelected.delete(taskId);
    showToast('已移入回收站','success');
    document.getElementById('task-detail-modal')?.remove();
    await loadTasks();
    _pruneTaskIdsFromAllTasks([taskId]);
    _startTaskPoll();
  }catch(e){showToast('操作失败: '+e.message,'error');}
}

async function restoreTaskRecord(taskId){
  if(!taskId)return;
  if(!confirm('确定恢复这条任务？'))return;
  try{
    const r=await api('POST','/tasks/restore-batch',{task_ids:[taskId]});
    if((r.restored||0)<1) showToast('未能恢复（可能已不在回收站）','warn');
    else showToast('已恢复','success');
    _taskBulkSelected.delete(taskId);
    document.getElementById('task-detail-modal')?.remove();
    await loadTasks();
  }catch(e){showToast('恢复失败: '+e.message,'error');}
}

async function eraseTaskRecord(taskId){
  if(!taskId)return;
  if(!confirm('永久删除这条记录？不可撤销。'))return;
  try{
    const r=await api('POST','/tasks/erase-batch',{task_ids:[taskId]});
    if((r.erased||0)<1) showToast('未能删除','warn');
    else showToast('已永久删除','success');
    _taskBulkSelected.delete(taskId);
    document.getElementById('task-detail-modal')?.remove();
    await loadTasks();
  }catch(e){showToast('删除失败: '+e.message,'error');}
}

async function _tdAction(action,taskId){
  const t=allTasks.find(x=>x.task_id===taskId);
  try{
    if(action==='cancel'){
      await api('POST',`/tasks/${taskId}/cancel`);
      showToast('任务已取消','success');
      document.getElementById('task-detail-modal')?.remove();
      await loadTasks();
      _startTaskPoll();
    } else if(action==='delete'){
      await deleteTaskRecord(taskId);
    } else if(action==='restore'){
      await restoreTaskRecord(taskId);
    } else if(action==='erase'){
      await eraseTaskRecord(taskId);
    } else if(action==='retry'&&t){
      await api('POST','/tasks',{type:t.type,device_id:t.device_id,params:t.params||{}});
      showToast('已重新提交','success');
      document.getElementById('task-detail-modal')?.remove();
      await loadTasks();
      _startTaskPoll();
    }
  }catch(e){showToast('操作失败: '+e.message,'error');}
}

/* Sprint C-1: 从 extract_members 任务一键创建 profile_hunt。
   预填 candidates_from_task_id=<上游任务 ID>，持 device 一致，
   命中画像的人由用户选"仅识别"/"自动关注"/"加好友"。 */
async function _tdLaunchHunt(upstreamTaskId, deviceId, memberCount){
  document.getElementById('task-detail-modal')?.remove();
  // 拉画像列表
  let personas=[];
  try {
    const r=await api('GET','/facebook/target-personas');
    personas=(r&&r.personas)||[];
  } catch(_){}
  const personaOpts=['<option value="">使用默认画像</option>']
    .concat(personas.map(p=>`<option value="${p.key}">${p.name||p.key}</option>`))
    .join('');
  const modal=document.createElement('div');
  modal.id='hunt-launch-modal';
  modal.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
  modal.innerHTML=`
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:22px;width:min(460px,95vw);box-shadow:0 20px 60px rgba(0,0,0,.4)">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
        <div style="font-size:16px;font-weight:700">🧠 从群成员创建画像识别</div>
        <button onclick="document.getElementById('hunt-launch-modal').remove()" style="background:none;border:none;color:var(--text-muted);font-size:20px;cursor:pointer">✕</button>
      </div>
      <div style="font-size:12px;color:var(--text-dim);margin-bottom:12px;padding:8px 10px;background:rgba(139,92,246,.08);border:1px solid rgba(139,92,246,.28);border-radius:8px">
        将对上游任务的 <b style="color:#a78bfa">${memberCount}</b> 个群成员批量跑 L1+L2 识别，命中目标画像的人可选择自动动作。
      </div>
      <div style="display:flex;flex-direction:column;gap:10px;font-size:13px">
        <div style="display:flex;align-items:center;gap:10px"><span style="min-width:90px">目标画像</span>
          <select id="hl-persona" style="flex:1;padding:6px 10px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px">${personaOpts}</select>
        </div>
        <div style="display:flex;align-items:center;gap:10px"><span style="min-width:90px">命中后动作</span>
          <select id="hl-action" style="flex:1;padding:6px 10px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px">
            <option value="none">仅识别（最安全）</option>
            <option value="follow">自动关注</option>
            <option value="add_friend">加好友</option>
          </select>
        </div>
        <div style="display:flex;align-items:center;gap:10px"><span style="min-width:90px">最多处理</span>
          <input id="hl-max" type="number" value="${Math.min(30,memberCount)}" style="width:80px;padding:5px 8px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px">
          <span style="font-size:11px;color:var(--text-dim)">/ ${memberCount}</span>
        </div>
        <div style="display:flex;align-items:center;gap:10px"><span style="min-width:90px">间隔(秒)</span>
          <input id="hl-interval" type="number" value="20" style="width:80px;padding:5px 8px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px">
          <span style="font-size:11px;color:var(--text-dim)">下限，真人节奏</span>
        </div>
      </div>
      <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:18px">
        <button class="qa-btn" onclick="document.getElementById('hunt-launch-modal').remove()">取消</button>
        <button class="qa-btn" style="background:#8b5cf6;color:#fff;border-color:#8b5cf6;font-weight:600" onclick="_tdLaunchHuntSubmit('${upstreamTaskId}','${deviceId}')">▶ 创建任务</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener('click',e=>{if(e.target===modal)modal.remove();});
}

async function _tdLaunchHuntSubmit(upstreamTaskId, deviceId){
  const persona=(document.getElementById('hl-persona')||{}).value||'';
  const action=(document.getElementById('hl-action')||{}).value||'none';
  const maxT=parseInt((document.getElementById('hl-max')||{}).value)||30;
  const interval=parseInt((document.getElementById('hl-interval')||{}).value)||20;
  try {
    await api('POST','/platforms/facebook/tasks',{
      task_type:'facebook_profile_hunt',
      device_id:deviceId,
      params:{
        candidates_from_task_id:upstreamTaskId,
        persona_key:persona,
        action_on_match:action,
        max_targets:maxT,
        inter_target_min_sec:interval,
      }
    });
    showToast('画像识别任务已创建','success');
    document.getElementById('hunt-launch-modal')?.remove();
    await loadTasks();
    _startTaskPoll();
  } catch(e) {
    showToast('创建失败: '+(e.message||e),'error');
  }
}

/* ── P9: 错误智能分析面板 ── */
const _CAT_LABELS = {
  vpn_failure: 'VPN失败', network_timeout: '网络超时', ui_not_found: 'UI未找到',
  account_limited: '账号限流', device_offline: '设备离线', geo_mismatch: 'IP不匹配',
  task_timeout: '任务超时', unknown: '其他',
};
const _CAT_COLORS = {
  vpn_failure: '#f97316', network_timeout: '#eab308', ui_not_found: '#3b82f6',
  account_limited: '#ef4444', device_offline: '#6b7280', geo_mismatch: '#a855f7',
  task_timeout: '#fb923c', unknown: '#4b5563',
};

async function _loadErrorAnalysis() {
  try {
    const d = await api('GET', '/tasks/error-analysis?hours=24&include_samples=true');
    const panel = document.getElementById('error-analysis-panel');
    if (!panel) return;

    const totalFailed = d.total_failed || 0;
    const failureRate = d.failure_rate || 0;
    const cats = d.categories || {};
    const alerts = d.alerts || [];
    const suggestions = d.suggestions || [];
    const trend = d.hourly_trend || [];

    // 隐藏面板：无失败时
    if (totalFailed === 0) { panel.style.display = 'none'; return; }
    panel.style.display = '';

    // 更新 header
    const badgeEl = document.getElementById('eap-badge-count');
    if (badgeEl) { badgeEl.textContent = totalFailed; badgeEl.style.display = ''; }
    const summaryEl = document.getElementById('eap-summary-text');
    const topCat = d.top_category || 'unknown';
    const topCount = cats[topCat] || 0;
    if (summaryEl) summaryEl.textContent = '\u8fc7\u53bb24h\uff1a\u4e3b\u8981 ' + (_CAT_LABELS[topCat] || topCat) + ' \xd7' + topCount;
    const rateEl = document.getElementById('eap-rate');
    if (rateEl) {
      rateEl.textContent = failureRate + '%';
      rateEl.style.color = failureRate > 30 ? 'var(--red)' : failureRate > 10 ? '#eab308' : 'var(--green)';
    }
    const iconEl = document.getElementById('eap-icon');
    if (iconEl) iconEl.textContent = alerts.some(a => a.level === 'critical') ? '\ud83d\udea8' : '\u26a0\ufe0f';

    // 有 critical 告警时自动展开
    if (alerts.some(a => a.level === 'critical') && !_eapOpen) _toggleEap();

    // 渲染告警横幅
    const alertsEl = document.getElementById('eap-alerts');
    if (alertsEl) {
      alertsEl.innerHTML = alerts.map(a => {
        const bg = a.level === 'critical' ? 'rgba(239,68,68,.12)' : 'rgba(234,179,8,.1)';
        const col = a.level === 'critical' ? 'var(--red)' : '#eab308';
        return '<div style="background:' + bg + ';border:1px solid ' + col + ';border-radius:6px;padding:6px 10px;margin-bottom:6px;font-size:11px;color:' + col + '">' + (a.level === 'critical' ? '\ud83d\udea8' : '\u26a0\ufe0f') + ' ' + esc(a.message) + '</div>';
      }).join('');
    }

    // 渲染错误类型条形图
    const catsEl = document.getElementById('eap-cats');
    if (catsEl) {
      const maxCnt = Math.max(...Object.values(cats), 1);
      catsEl.innerHTML = Object.entries(cats)
        .filter(([, v]) => v > 0)
        .sort((a, b) => b[1] - a[1])
        .map(([cat, cnt]) => {
          const pct = Math.round(cnt / maxCnt * 100);
          const col = _CAT_COLORS[cat] || '#6b7280';
          return '<div style="margin-bottom:5px"><div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:2px"><span style="color:var(--text-dim)">' + (_CAT_LABELS[cat] || cat) + '</span><span style="font-weight:600;color:' + col + '">' + cnt + '</span></div><div style="background:var(--bg-card);border-radius:3px;height:6px;overflow:hidden"><div style="width:' + pct + '%;height:100%;background:' + col + ';border-radius:3px;transition:width .4s"></div></div></div>';
        }).join('') || '<div style="color:var(--text-dim);font-size:11px">\u6682\u65e0\u9519\u8bef\u6570\u636e</div>';
    }

    // 渲染修复建议
    const suggEl = document.getElementById('eap-suggestions');
    if (suggEl) {
      suggEl.innerHTML = suggestions.length ? suggestions.map(s => {
        const priColor = s.priority === 'critical' || s.priority === 'high' ? 'var(--red)' : s.priority === 'medium' ? '#eab308' : 'var(--text-dim)';
        const btnHtml = s.endpoint ? '<button class="qa-btn" onclick="_eapAction(\'' + (s.method||'POST') + '\',\'' + s.endpoint + '\')" style="font-size:10px;padding:2px 8px;margin-top:4px;border-color:' + priColor + ';color:' + priColor + '">\u6267\u884c</button>' : '';
        return '<div style="background:var(--bg-card);border-radius:6px;padding:7px 9px;margin-bottom:6px;border-left:3px solid ' + priColor + '"><div style="font-size:12px">' + (s.icon || '') + ' ' + esc(s.action) + '</div>' + btnHtml + '</div>';
      }).join('') : '<div style="color:var(--text-dim);font-size:11px">\u65e0\u5efa\u8bae</div>';
    }

    // 渲染 unknown 错误样本
    const samples = d.samples || {};
    const unknownSamples = samples['unknown'] || [];
    if (unknownSamples.length > 0) {
      const unknownEl = document.getElementById('eap-unknown-samples');
      if (unknownEl) {
        unknownEl.innerHTML = unknownSamples.map(s =>
          `<div style="font-size:10px;color:var(--text-dim);padding:3px 6px;background:var(--bg-card);border-radius:4px;margin-bottom:3px;font-family:monospace">${esc(s)}</div>`
        ).join('');
        unknownEl.parentElement.style.display = '';
      }
    }

    // 渲染24小时趋势迷你柱
    const trendEl = document.getElementById('eap-trend');
    if (trendEl && trend.length) {
      const maxF = Math.max(...trend.map(t => t.failed), 1);
      trendEl.innerHTML = trend.map(t => {
        const h = Math.max(Math.round(t.failed / maxF * 36), t.failed > 0 ? 3 : 1);
        const col = t.rate > 30 ? 'var(--red)' : t.rate > 10 ? '#eab308' : 'var(--green)';
        const hr = (t.hour || '').substring(11, 16);
        return '<div title="' + hr + ' \u5931\u8d25' + t.failed + '/' + t.total + '" style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px"><div style="width:100%;max-width:16px;height:' + h + 'px;background:' + col + ';border-radius:2px 2px 0 0;min-height:2px"></div><span style="font-size:8px;color:var(--text-dim)">' + hr + '</span></div>';
      }).join('');
    }
  } catch(e) { console.debug('error analysis failed', e); }
}

function _toggleEap() {
  _eapOpen = !_eapOpen;
  const detail = document.getElementById('eap-detail');
  const chev = document.getElementById('eap-chevron');
  if (detail) detail.style.display = _eapOpen ? '' : 'none';
  if (chev) chev.style.transform = _eapOpen ? 'rotate(180deg)' : '';
  if (_eapOpen) _loadErrorAnalysis();
}

async function _eapAction(method, endpoint) {
  try {
    await api(method, endpoint);
    showToast('\u64cd\u4f5c\u5df2\u6267\u884c', 'success');
    setTimeout(_loadErrorAnalysis, 1500);
  } catch(e) { showToast('\u64cd\u4f5c\u5931\u8d25: ' + (e.message || e), 'error'); }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// ★ P3-1: 今日涨粉仪表盘（60s 轮询）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
let _growthTimer = null;

async function _loadGrowthStats() {
  const dashboard = document.getElementById('growth-dashboard');
  if (!dashboard || dashboard.offsetParent === null) return; // 不可见时跳过
  try {
    const data = await api('GET', '/tiktok/growth_stats');
    if (!data || !data.summary) return;

    const s = data.summary;
    const el = id => document.getElementById(id);

    // KPI 数字
    const kFollowed = el('gk-followed');
    const kFans = el('gk-fans');
    const kQuota = el('gk-quota');
    if (kFollowed) kFollowed.textContent = s.total_followed_today ?? '-';
    if (kFans) kFans.textContent = `+${s.est_total_fans_today ?? 0}`;
    const quotaLeft = (s.total_quota || 0) - (s.total_followed_today || 0);
    if (kQuota) kQuota.textContent = quotaLeft >= 0 ? quotaLeft : '满';

    // 设备列表迷你进度条
    const listEl = el('growth-device-list');
    if (listEl && data.devices) {
      const shortId = d => {
        if (d.includes(':')) return d.split(':')[0].split('.').slice(-1)[0]; // IP last octet
        return d.substring(0, 6);
      };
      listEl.innerHTML = data.devices.map(d => {
        const pct = d.quota_pct || 0;
        const barCls = pct >= 90 ? 'danger' : pct >= 70 ? 'warn' : '';
        const pauseIcon = d.should_pause ? ' ⏸' : '';
        const phaseShort = {'cold_start':'冷启动','interest_building':'建兴趣','follow_unlocked':'可关注','scaling':'扩量','unknown':'?'}[d.phase] || d.phase;
        return `<div class="growth-dev-row">
          <span style="width:28px;color:${d.should_pause?'#f87171':'var(--text-muted)'}">${shortId(d.device_id)}${pauseIcon}</span>
          <div class="growth-bar-wrap"><div class="growth-bar ${barCls}" style="width:${Math.min(pct,100)}%"></div></div>
          <span style="width:32px;text-align:right;color:${d.should_pause?'#f87171':'var(--text-muted)'}">${d.followed_today}/${d.quota_max}</span>
          <span style="width:36px;color:var(--text-muted);font-size:8px">${phaseShort}</span>
        </div>`;
      }).join('');
    }

    // 最后更新时间
    const now = new Date();
    const timeStr = `${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}`;
    const lu = el('growth-last-update');
    if (lu) lu.textContent = `更新 ${timeStr}`;

    // 超配额提醒
    if (s.paused_count > 0) {
      showToast(`⚠ ${s.paused_count} 台设备今日关注已达配额上限`, 'warn');
    }
  } catch(e) {
    // 静默失败，不打扰用户
  }
}

function _startGrowthDashboard() {
  _loadGrowthStats();
  _growthTimer = setInterval(_loadGrowthStats, 60000);
}

// task.failed WS事件触发刷新（10s debounce）
function _eapOnTaskFailed() {
  if (_eapDebounceTimer) clearTimeout(_eapDebounceTimer);
  _eapDebounceTimer = setTimeout(() => {
    _loadErrorAnalysis();
  }, 10000);
}
