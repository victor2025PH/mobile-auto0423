/* platforms.js — 社交平台: AI操控、社交平台控制（Facebook/Instagram/LinkedIn/TikTok/Twitter） */
/* ── AI 操控助手 ── */
function clearAiChat(){document.getElementById('ai-chat-output').innerHTML='';}
function _appendAiMsg(role,text){
  const out=document.getElementById('ai-chat-output');
  if(!out) return;
  const div=document.createElement('div');
  div.style.cssText=`margin-bottom:8px;padding:6px 10px;border-radius:8px;font-size:12px;max-width:85%;word-break:break-word;${
    role==='user'?'background:#1e3a5f;color:#93c5fd;margin-left:auto;text-align:right':'background:#1a1a2e;color:#e2e8f0'
  }`;
  div.textContent=text;
  out.appendChild(div);
  out.scrollTop=out.scrollHeight;
}
async function sendAiCmd(){
  const inp=document.getElementById('ai-input');
  const text=inp?.value?.trim();if(!text) return;
  inp.value='';
  _appendAiMsg('user',text);
  _appendAiMsg('system','处理中...');
  try{
    const r=await api('POST','/ai/execute-intent',{
      instruction:text, device_id:_currentModalDevice
    });
    const out=document.getElementById('ai-chat-output');
    if(out) out.lastChild.remove();
    if(r.reply) _appendAiMsg('assistant',r.reply);
    if(r.commands?.length){
      _appendAiMsg('system',`执行了 ${r.commands.length} 条命令`);
      for(const cmd of r.commands){
        _appendAiMsg('system',`$ ${cmd.command}\n${cmd.output||'(ok)'}`);
      }
    }
    if(r.error) _appendAiMsg('system','错误: '+r.error);
  }catch(e){
    const out=document.getElementById('ai-chat-output');
    if(out?.lastChild) out.lastChild.remove();
    _appendAiMsg('system','请求失败: '+e.message);
  }
}
function aiQuickCmd(text){
  const inp=document.getElementById('ai-input');
  if(inp) inp.value=text;
  sendAiCmd();
}

/* ── 社交平台控制面板 ── */
const PLAT_ICONS={tiktok:'&#127916;',telegram:'&#9992;',whatsapp:'&#128172;',facebook:'&#128101;',linkedin:'&#128188;',instagram:'&#128247;',twitter:'&#120143;'};
const PLAT_COLORS={tiktok:'#ff0050',telegram:'#0088cc',whatsapp:'#25d366',facebook:'#1877f2',linkedin:'#0a66c2',instagram:'#e4405f',twitter:'#1d9bf0'};
const PLAT_SLOGANS={tiktok:'智能养号 & 精准引流',telegram:'消息自动化 & 社群运营',whatsapp:'客户沟通 & 消息触达',facebook:'社交拓客 & 内容分发',linkedin:'B2B人脉拓展 & 商务触达',instagram:'视觉种草 & 红人互动',twitter:'话题营销 & 公域声量'};
const PLAT_FLOWS={
  tiktok:{steps:[{n:'养号',d:'Day1-3',c:'#22c55e'},{n:'关注',d:'Day4-6',c:'#3b82f6'},{n:'聊天引流',d:'Day7+',c:'#f59e0b'}],tips:['&#127793; <b>冷启动期</b>: 每天刷视频+随机互动，建立账号权重','&#128101; <b>兴趣建立</b>: 搜索目标用户，智能筛选+精准关注','&#128172; <b>活跃转化</b>: 回关用户自动聊天，引导到私域']},
  telegram:{steps:[{n:'加群',d:'Step1',c:'#0088cc'},{n:'发消息',d:'Step2',c:'#22c55e'},{n:'自动回复',d:'Step3',c:'#f59e0b'}],tips:['&#128101; <b>加入群组</b>: 搜索目标群组，自动申请加入','&#9993; <b>主动触达</b>: 批量发送个性化消息，AI改写避重复','&#129302; <b>自动回复</b>: 7x24后台运行，自动响应新消息']},
  whatsapp:{steps:[{n:'导入联系人',d:'Step1',c:'#25d366'},{n:'发消息',d:'Step2',c:'#3b82f6'},{n:'跟进',d:'Step3',c:'#f59e0b'}],tips:['&#128222; <b>导入联系人</b>: 批量添加目标客户号码','&#9993; <b>消息触达</b>: 文字+图片+视频多媒体消息','&#128260; <b>持续跟进</b>: 自动回复+定时跟进，提升转化']},
  facebook:{steps:[{n:'加好友',d:'Step1',c:'#1877f2'},{n:'互动',d:'Step2',c:'#22c55e'},{n:'引流',d:'Step3',c:'#f59e0b'}],tips:['&#129309; <b>拓展人脉</b>: 精准搜索+批量加好友','&#128240; <b>内容互动</b>: 浏览、点赞、评论，提升可见度','&#128172; <b>私信引流</b>: 主动DM，引导至私域或落地页']},
  linkedin:{steps:[{n:'搜人脉',d:'Step1',c:'#0a66c2'},{n:'建联',d:'Step2',c:'#22c55e'},{n:'转化',d:'Step3',c:'#f59e0b'}],tips:['&#128269; <b>精准搜索</b>: 按职位/行业/地区搜索B2B人脉','&#129309; <b>批量建联</b>: 发送个性化邀请+自动接受回邀','&#128172; <b>商务转化</b>: 消息跟进，引导会议或合作']},
  instagram:{steps:[{n:'浏览种草',d:'Step1',c:'#e4405f'},{n:'互动关注',d:'Step2',c:'#f59e0b'},{n:'DM引流',d:'Step3',c:'#8b5cf6'}],tips:['&#128247; <b>视觉种草</b>: 浏览标签+首页，建立兴趣画像','&#10084; <b>精准互动</b>: 搜索目标用户，点赞+关注','&#128172; <b>私信转化</b>: 批量DM，引导至私域']},
  twitter:{steps:[{n:'浏览话题',d:'Step1',c:'#1d9bf0'},{n:'搜索互动',d:'Step2',c:'#22c55e'},{n:'DM触达',d:'Step3',c:'#f59e0b'}],tips:['&#128240; <b>话题追踪</b>: 浏览时间线，建立账号权重','&#128269; <b>关键词互动</b>: 按关键词搜索+互动，精准触达目标','&#128172; <b>私信引流</b>: 向高意向用户发送DM']}
};
const PLAT_TASK_ICONS={tiktok_warmup:'&#127793;',tiktok_watch:'&#128250;',tiktok_follow:'&#128101;',tiktok_send_dm:'&#128172;',tiktok_check_inbox:'&#128229;',tiktok_acquisition:'&#128640;',telegram_send_message:'&#9993;',telegram_read_messages:'&#128196;',telegram_send_file:'&#128206;',telegram_workflow:'&#9881;',telegram_auto_reply:'&#129302;',telegram_join_group:'&#128101;',telegram_send_group:'&#128227;',telegram_monitor_chat:'&#128065;',whatsapp_send_message:'&#9993;',whatsapp_read_messages:'&#128196;',whatsapp_auto_reply:'&#129302;',whatsapp_send_media:'&#127909;',whatsapp_list_chats:'&#128203;',facebook_send_message:'&#9993;',facebook_add_friend:'&#129309;',facebook_browse_feed:'&#128240;',facebook_browse_feed_by_interest:'&#128293;',facebook_search_leads:'&#128269;',facebook_join_group:'&#128101;',facebook_profile_hunt:'&#127919;',linkedin_send_message:'&#9993;',linkedin_read_messages:'&#128196;',linkedin_post_update:'&#128221;',linkedin_search_profile:'&#128269;',linkedin_send_connection:'&#129309;',linkedin_accept_connections:'&#9989;',linkedin_like_post:'&#10084;',linkedin_comment_post:'&#128172;',instagram_browse_feed:'&#128247;',instagram_browse_hashtag:'&#128278;',instagram_search_leads:'&#128269;',instagram_send_dm:'&#128172;',twitter_browse_timeline:'&#128240;',twitter_search_leads:'&#128269;',twitter_search_and_engage:'&#128260;',twitter_send_dm:'&#128172;'};
const PLAT_TASK_GRADIENTS={tiktok_warmup:'linear-gradient(135deg,#22c55e,#16a34a)',tiktok_watch:'linear-gradient(135deg,#8b5cf6,#6366f1)',tiktok_follow:'linear-gradient(135deg,#3b82f6,#2dd4bf)',tiktok_send_dm:'linear-gradient(135deg,#f59e0b,#ef4444)',tiktok_check_inbox:'linear-gradient(135deg,#06b6d4,#3b82f6)',tiktok_acquisition:'linear-gradient(135deg,#ef4444,#f97316)',telegram_join_group:'linear-gradient(135deg,#0088cc,#00afd4)',telegram_send_message:'linear-gradient(135deg,#22c55e,#16a34a)',telegram_send_file:'linear-gradient(135deg,#3b82f6,#6366f1)',telegram_auto_reply:'linear-gradient(135deg,#f59e0b,#f97316)',telegram_send_group:'linear-gradient(135deg,#06b6d4,#0088cc)',telegram_read_messages:'linear-gradient(135deg,#8b5cf6,#a78bfa)',telegram_monitor_chat:'linear-gradient(135deg,#6366f1,#8b5cf6)',whatsapp_list_chats:'linear-gradient(135deg,#25d366,#22c55e)',whatsapp_read_messages:'linear-gradient(135deg,#16a34a,#22c55e)',whatsapp_send_message:'linear-gradient(135deg,#3b82f6,#2dd4bf)',whatsapp_send_media:'linear-gradient(135deg,#8b5cf6,#6366f1)',whatsapp_auto_reply:'linear-gradient(135deg,#f59e0b,#f97316)',facebook_browse_feed:'linear-gradient(135deg,#1877f2,#3b82f6)',facebook_browse_feed_by_interest:'linear-gradient(135deg,#f97316,#ec4899)',facebook_search_leads:'linear-gradient(135deg,#06b6d4,#1877f2)',facebook_add_friend:'linear-gradient(135deg,#22c55e,#16a34a)',facebook_join_group:'linear-gradient(135deg,#8b5cf6,#1877f2)',facebook_send_message:'linear-gradient(135deg,#f59e0b,#f97316)',linkedin_search_profile:'linear-gradient(135deg,#0a66c2,#0088cc)',linkedin_accept_connections:'linear-gradient(135deg,#22c55e,#0a66c2)',linkedin_send_connection:'linear-gradient(135deg,#16a34a,#22c55e)',linkedin_post_update:'linear-gradient(135deg,#3b82f6,#0a66c2)',linkedin_like_post:'linear-gradient(135deg,#ef4444,#f97316)',linkedin_comment_post:'linear-gradient(135deg,#8b5cf6,#6366f1)',linkedin_send_message:'linear-gradient(135deg,#f59e0b,#ef4444)',linkedin_read_messages:'linear-gradient(135deg,#06b6d4,#0a66c2)',instagram_browse_feed:'linear-gradient(135deg,#e4405f,#f97316)',instagram_browse_hashtag:'linear-gradient(135deg,#8b5cf6,#e4405f)',instagram_search_leads:'linear-gradient(135deg,#06b6d4,#8b5cf6)',instagram_send_dm:'linear-gradient(135deg,#f59e0b,#e4405f)',twitter_browse_timeline:'linear-gradient(135deg,#1d9bf0,#3b82f6)',twitter_search_leads:'linear-gradient(135deg,#06b6d4,#1d9bf0)',twitter_search_and_engage:'linear-gradient(135deg,#22c55e,#1d9bf0)',twitter_send_dm:'linear-gradient(135deg,#f59e0b,#1d9bf0)'};
let _platRunningTasks={};let _platAllTasks=[];
// TikTok任务三阶段分组
const PLAT_PHASES={
  tiktok:[
    {label:'① 基础养成',desc:'建立账号权重，提升算法分',color:'#22c55e',types:['tiktok_warmup','tiktok_watch']},
    {label:'② 扩大曝光',desc:'精准关注目标用户，触发回关',color:'#3b82f6',types:['tiktok_follow','tiktok_check_and_chat_followbacks']},
    {label:'③ 获客转化',desc:'检测回复，AI引流到私域',color:'#ef4444',types:['tiktok_check_inbox','tiktok_acquisition'],highlight:true}
  ],
  telegram:[
    {label:'① 群组渗透',desc:'搜索加入目标群组，积累曝光',color:'#0088cc',types:['telegram_join_group','telegram_send_group']},
    {label:'② 主动触达',desc:'精准私信+批量发送+文件推送',color:'#22c55e',types:['telegram_send_message','telegram_send_file']},
    {label:'③ 自动运营',desc:'自动回复+持续监控',color:'#f59e0b',types:['telegram_auto_reply','telegram_read_messages','telegram_monitor_chat'],highlight:true}
  ],
  whatsapp:[
    {label:'① 联系人管理',desc:'获取聊天列表，整理目标客户',color:'#25d366',types:['whatsapp_list_chats','whatsapp_read_messages']},
    {label:'② 消息推送',desc:'文字+多媒体批量触达',color:'#3b82f6',types:['whatsapp_send_message','whatsapp_send_media']},
    {label:'③ 自动跟进',desc:'7x24自动回复，持续转化',color:'#f59e0b',types:['whatsapp_auto_reply'],highlight:true}
  ],
  facebook:[
    {label:'① 浏览互动',desc:'浏览动态+按画像兴趣刷帖+搜索潜客',color:'#1877f2',types:['facebook_browse_feed','facebook_browse_feed_by_interest','facebook_search_leads']},
    {label:'② 画像识别',desc:'批量识别候选+自动关注',color:'#8b5cf6',types:['facebook_profile_hunt']},
    {label:'③ 人脉拓展',desc:'批量加好友+加入群组',color:'#22c55e',types:['facebook_add_friend','facebook_join_group']},
    {label:'④ 私信转化',desc:'DM精准触达，引导到私域',color:'#f59e0b',types:['facebook_send_message'],highlight:true}
  ],
  linkedin:[
    {label:'① 人脉搜索',desc:'按职位/行业/地区精准搜索',color:'#0a66c2',types:['linkedin_search_profile','linkedin_accept_connections']},
    {label:'② 关系建立',desc:'发邀请+发动态+互动',color:'#22c55e',types:['linkedin_send_connection','linkedin_post_update','linkedin_like_post','linkedin_comment_post']},
    {label:'③ 商务触达',desc:'精准消息+持续跟进',color:'#f59e0b',types:['linkedin_send_message','linkedin_read_messages'],highlight:true}
  ],
  instagram:[
    {label:'① 视觉浏览',desc:'首页+标签浏览，建立兴趣画像',color:'#e4405f',types:['instagram_browse_feed','instagram_browse_hashtag']},
    {label:'② 精准获客',desc:'搜索目标用户，批量入库',color:'#f59e0b',types:['instagram_search_leads']},
    {label:'③ DM引流',desc:'批量私信，引导至私域',color:'#8b5cf6',types:['instagram_send_dm'],highlight:true}
  ],
  twitter:[
    {label:'① 话题浏览',desc:'浏览时间线，建立账号画像',color:'#1d9bf0',types:['twitter_browse_timeline']},
    {label:'② 搜索互动',desc:'关键词搜索用户+互动',color:'#22c55e',types:['twitter_search_leads','twitter_search_and_engage']},
    {label:'③ DM触达',desc:'向高意向用户发送私信',color:'#f59e0b',types:['twitter_send_dm'],highlight:true}
  ]
};
const PLAT_TK_PHASES=PLAT_PHASES.tiktok;
// 任务参数配置（用于确认弹窗）
const PLAT_TK_PARAMS={
  tiktok_check_inbox:[{key:'max_conversations',label:'最多处理对话',type:'number',default:50},{key:'auto_reply',label:'自动回复',type:'checkbox',default:true}],
  tiktok_follow:[{key:'max_follows',label:'最多关注人数',type:'number',default:30}],
  tiktok_warmup:[{key:'duration_seconds',label:'预热时长(秒)',type:'number',default:1800}],
  tiktok_check_and_chat_followbacks:[{key:'max_chats',label:'最多私信人数',type:'number',default:10}],
  telegram_join_group:[{key:'group',label:'群组名/链接',type:'text',default:''}],
  telegram_send_group:[{key:'group',label:'群组名/链接',type:'text',default:''},{key:'message',label:'消息内容',type:'text',default:''}],
  telegram_auto_reply:[{key:'duration',label:'运行时长(分钟)',type:'number',default:60}],
  telegram_monitor_chat:[{key:'username',label:'监控用户',type:'text',default:''},{key:'duration',label:'时长(分钟)',type:'number',default:30}],
  whatsapp_auto_reply:[{key:'duration',label:'运行时长(分钟)',type:'number',default:60}],
  whatsapp_send_message:[{key:'contact',label:'联系人/手机号',type:'text',default:''},{key:'message',label:'消息内容',type:'text',default:''}],
  telegram_send_message:[{key:'username',label:'用户名/@handle',type:'text',default:''},{key:'message',label:'消息内容',type:'text',default:''}],
  facebook_browse_feed:[{key:'duration',label:'浏览时长(分钟)',type:'number',default:15}],
  facebook_browse_feed_by_interest:[
    {key:'duration',label:'总时长(分钟)',type:'number',default:15},
    {key:'persona_key',label:'画像 key(过滤兴趣)',type:'text',default:'jp_female_midlife'},
    {key:'interest_hours',label:'兴趣统计窗口(小时)',type:'number',default:168},
    {key:'max_topics',label:'最多几个 topic',type:'number',default:4},
    {key:'like_boost',label:'点赞概率加成(0~0.3)',type:'number',default:0.12}
  ],
  facebook_search_leads:[{key:'keyword',label:'搜索关键词',type:'text',default:''}],
  facebook_join_group:[{key:'group_name',label:'群组名称',type:'text',default:''}],
  facebook_add_friend:[{key:'target',label:'目标用户(主页/ID)',type:'text',default:''},{key:'note',label:'附言(可选)',type:'text',default:''}],
  facebook_profile_hunt:[
    {key:'candidates',label:'候选名字(每行一个)',type:'textarea',default:'',placeholder:'山田花子\n佐藤美恵\nMiyuki Tanaka'},
    {key:'persona_key',label:'目标画像',type:'select_persona',default:''},
    {key:'action_on_match',label:'命中后动作',type:'select',options:[['none','仅识别(最安全)'],['follow','自动关注'],['add_friend','加好友']],default:'none'},
    {key:'max_targets',label:'本次最多处理',type:'number',default:30},
    {key:'inter_target_min_sec',label:'间隔下限(秒)',type:'number',default:20},
    {key:'shot_count',label:'截图张数(L2用)',type:'number',default:3}
  ],
  facebook_send_message:[{key:'target',label:'目标用户(主页/ID)',type:'text',default:''},{key:'message',label:'消息内容',type:'text',default:''}],
  instagram_send_dm:[{key:'recipient',label:'目标用户名',type:'text',default:''},{key:'message',label:'消息内容',type:'text',default:''}],
  twitter_send_dm:[{key:'recipient',label:'目标用户名',type:'text',default:''},{key:'message',label:'消息内容',type:'text',default:''}],
  linkedin_search_profile:[{key:'query',label:'搜索关键词',type:'text',default:''}],
  linkedin_send_connection:[{key:'name',label:'目标姓名',type:'text',default:''},{key:'note',label:'邀请附言',type:'text',default:''}],
  linkedin_accept_connections:[{key:'max_accept',label:'最多接受数',type:'number',default:20}],
  linkedin_post_update:[{key:'content',label:'动态内容',type:'text',default:''}],
  instagram_browse_feed:[{key:'scroll_count',label:'滑动次数',type:'number',default:30},{key:'like_probability',label:'点赞概率(0-1)',type:'number',default:0.15}],
  instagram_browse_hashtag:[{key:'hashtag',label:'标签(含#)',type:'text',default:''},{key:'scroll_count',label:'滑动次数',type:'number',default:20}],
  instagram_search_leads:[{key:'keyword',label:'搜索关键词',type:'text',default:''},{key:'max_leads',label:'最多入库数',type:'number',default:10}],
  twitter_browse_timeline:[{key:'scroll_count',label:'滑动次数',type:'number',default:30},{key:'like_probability',label:'点赞概率(0-1)',type:'number',default:0.15}],
  twitter_search_leads:[{key:'keyword',label:'搜索关键词',type:'text',default:''},{key:'max_leads',label:'最多入库数',type:'number',default:10}],
  twitter_search_and_engage:[{key:'keyword',label:'关键词',type:'text',default:''},{key:'max_tweets',label:'最多互动数',type:'number',default:15}],
};
/** TikTok 任务弹窗中展示「人群预设」下拉（与 /task-params/audience-presets 同步） */
const PLAT_TK_AUDIENCE_TASKS=new Set(['tiktok_follow','tiktok_warmup','tiktok_check_inbox','tiktok_check_and_chat_followbacks','tiktok_watch']);
/** { list: [], etag: string } | undefined */
let _audiencePresetsCache=undefined;
let _audiencePresetsInflight=null;
function platTkNeedsAudienceModal(taskType){
  return PLAT_TK_AUDIENCE_TASKS.has(taskType);
}
async function _loadAudiencePresetsCached(force){
  if(force) _audiencePresetsCache=undefined;
  if(_audiencePresetsInflight) return _audiencePresetsInflight;
  _audiencePresetsInflight=(async()=>{
    try{
      var url='/task-params/audience-presets';
      if(_audiencePresetsCache&&_audiencePresetsCache.etag&&!force){
        url+='?if_etag='+encodeURIComponent(_audiencePresetsCache.etag);
      }
      var r=await api('GET',url);
      if(r&&r.unchanged&&_audiencePresetsCache){
        return _audiencePresetsCache.list;
      }
      var list=(r&&r.presets)?r.presets:[];
      var etag=(r&&r.etag)?String(r.etag):'';
      _audiencePresetsCache={list:Array.isArray(list)?list:[],etag:etag};
    }catch(e){
      console.warn('[audience-presets]',e);
      if(!_audiencePresetsCache) _audiencePresetsCache={list:[],etag:''};
    }finally{
      _audiencePresetsInflight=null;
    }
    return _audiencePresetsCache?_audiencePresetsCache.list:[];
  })();
  return _audiencePresetsInflight;
}
function _ocSeedAudiencePresetsCache(list, etag){
  _audiencePresetsCache={list:Array.isArray(list)?list:[],etag:String(etag||'')};
}
/**
 * POST 重载 audience_presets.yaml，成功后写入本模块缓存与 flow 客户端种子（overview 已挂 _ttFlowAudienceClientSeed 时）。
 * 无有效 presets+etag 时走 invalidate，与流程/ops 刷新一致。返回 { presets, etag } 供调用方刷新 UI。
 */
async function _ocReloadAndSeedAudiencePresets(){
  var r=await api('POST','/task-params/reload-audience-presets',{});
  var presets=(r&&r.presets)?r.presets:[];
  var etag=(r&&r.etag)?String(r.etag):'';
  if(presets.length&&etag){
    _ocSeedAudiencePresetsCache(presets,etag);
    if(typeof window._ttFlowAudienceClientSeed==='function'){
      window._ttFlowAudienceClientSeed(presets,etag);
    }
  }else if(typeof window._ocInvalidateAudiencePresetsCache==='function'){
    window._ocInvalidateAudiencePresetsCache();
  }
  return { presets: presets, etag: etag };
}
async function _platTkRefreshAudienceSelect(){
  try{
    var out=await _ocReloadAndSeedAudiencePresets();
    var presets=out.presets;
    const sel=document.getElementById('tkp-audience_preset');
    if(!sel) return;
    const cur=sel.value;
    sel.innerHTML=['<option value="">（不使用人群预设）</option>'].concat(
      presets.map(p=>{
        const id=String((p&&p.id)||'').replace(/"/g,'&quot;');
        const lab=String((p&&p.label)||(p&&p.id)||'').replace(/</g,'&lt;').replace(/"/g,'&quot;');
        return `<option value="${id}">${lab}</option>`;
      })
    ).join('');
    if(cur&&[...sel.options].some(o=>o.value===cur)) sel.value=cur;
    if(typeof showToast==='function') showToast('预设列表已更新','success');
  }catch(e){
    console.warn('[audience-presets] refresh',e);
    if(typeof showToast==='function') showToast('刷新失败','error');
  }
}
const PLAT_TASK_HINTS={tiktok_warmup:'~30分钟/台',tiktok_watch:'~15分钟/台',tiktok_follow:'20-30人/台',tiktok_send_dm:'需输入内容',tiktok_check_inbox:'50条对话/次',tiktok_acquisition:'全自动引流',tiktok_check_and_chat_followbacks:'回关→自动私信',telegram_send_message:'需输入内容',telegram_read_messages:'自动读取',telegram_send_file:'需选择文件',telegram_workflow:'自动工作流',telegram_auto_reply:'后台运行',telegram_join_group:'需输入群名',telegram_send_group:'需输入内容',telegram_monitor_chat:'后台监控',whatsapp_send_message:'需输入内容',whatsapp_read_messages:'自动读取',whatsapp_auto_reply:'后台运行',whatsapp_send_media:'需选择文件',whatsapp_list_chats:'自动列出',facebook_send_message:'需输入内容',facebook_add_friend:'需输入用户',facebook_browse_feed:'15分钟/台',facebook_browse_feed_by_interest:'读画像兴趣→搜索页刷帖',facebook_search_leads:'需输入关键词',facebook_join_group:'需输入群名',facebook_profile_hunt:'AI识别目标',linkedin_send_message:'需输入内容',linkedin_read_messages:'自动读取',linkedin_post_update:'需输入内容',linkedin_search_profile:'需输入关键词',linkedin_send_connection:'批量发邀请',linkedin_accept_connections:'自动接受',linkedin_like_post:'浏览时点赞',linkedin_comment_post:'需输入内容',instagram_browse_feed:'~15分钟/台',instagram_browse_hashtag:'需输入标签',instagram_search_leads:'需输入关键词',instagram_send_dm:'需输入内容',twitter_browse_timeline:'~15分钟/台',twitter_search_leads:'需输入关键词',twitter_search_and_engage:'关键词互动',twitter_send_dm:'需输入内容'};
async function _fetchPlatTasks(platform){
  try{
    const [running,all]=await Promise.all([
      api('GET','/tasks?status=running'),
      api('GET','/tasks?limit=20')
    ]);
    const rArr=Array.isArray(running)?running:(running.tasks||[]);
    _platRunningTasks={};
    rArr.forEach(t=>{const tt=t.type||'';if(tt.startsWith(platform+'_')){_platRunningTasks[tt]=(_platRunningTasks[tt]||0)+1;}});
    const aArr=Array.isArray(all)?all:(all.tasks||[]);
    _platAllTasks=aArr.filter(t=>(t.type||'').startsWith(platform+'_'));
  }catch(e){_platRunningTasks={};_platAllTasks=[];}
}
async function _getOnlineDeviceCount(){
  let n=allDevices.filter(d=>d.status==='connected'||d.status==='online').length;
  let total=allDevices.length;
  if(_clusterDevices&&_clusterDevices.length){
    const localIds=new Set(allDevices.map(d=>d.device_id));
    const remote=_clusterDevices.filter(d=>!localIds.has(d.device_id));
    remote.forEach(d=>{total++;if(d.status==='connected'||d.status==='online')n++;});
  }
  if(total===0){
    try{
      const ov=await api('GET','/cluster/overview');
      if(ov&&ov.total_devices){total=ov.total_devices;n=ov.total_devices_online||0;}
    }catch(e){}
  }
  return {online:n,total:total};
}
async function loadPlatformPage(platform){
  const container=document.querySelector(`#page-plat-${platform} .plat-page`);
  if(!container) return;
  try{
    const [info,stats,,funnelData,alertsData,dailyReport]=await Promise.all([
      api('GET',`/platforms/${platform}`),
      api('GET',`/platforms/${platform}/stats?days=7`),
      _fetchPlatTasks(platform),
      platform==='tiktok'?api('GET','/tiktok/funnel').catch(()=>null):Promise.resolve(null),
      platform==='tiktok'?api('GET','/health/alerts?limit=30').catch(()=>null):Promise.resolve(null),
      platform==='tiktok'?api('GET','/tiktok/daily-report').catch(()=>null):Promise.resolve(null),
    ]);
    const tasks=info.task_types||[];
    const color=PLAT_COLORS[platform]||'#60a5fa';
    const icon=PLAT_ICONS[platform]||'';
    const slogan=PLAT_SLOGANS[platform]||'';
    const dc=await _getOnlineDeviceCount();
    const totalRunning=Object.values(_platRunningTasks).reduce((a,b)=>a+b,0);
    const todayTasks=_platAllTasks.filter(t=>{const d=t.created_at||0;return d>(Date.now()/1000-86400);});
    const todayDone=todayTasks.filter(t=>t.status==='completed').length;
    // ── TikTok 今日战报条 + 设备告警横幅 ──
    let kpiBarHtml='',alertBannerHtml='';
    if(platform==='tiktok'){
      const fd=funnelData||{};
      const dr=(dailyReport&&dailyReport.total)||{};
      // 今日数据 (来自 daily-report)
      const tkFollowedToday=dr.followed_today||0;
      const tkDmsToday=dr.dms_today||0;
      const tkWatchedToday=dr.watched||0;
      const tkLikedToday=dr.liked||0;
      // 累计漏斗数据 (来自 funnel)
      const tkFollowed=fd.total_followed||0;
      const tkDms=fd.total_dms||0;
      const tkFollowBacks=fd.follow_backs||0;
      const tkLeadsResp=fd.leads_responded||0;
      const tkQualified=fd.leads_qualified||0;
      const tkConverted=fd.leads_converted||0;
      const rev=dailyReport?.revenue||{};
      const tkRevTotal=rev.total_revenue||0;const tkRevToday=rev.today_revenue||0;
      const tkConvToday=rev.today_conversions||0;
      const followBackRate=tkFollowed>0?(tkFollowBacks/tkFollowed*100).toFixed(1):0;
      // 告警
      const replyWarn=tkDms>10&&tkLeadsResp===0;
      const followBackWarn=tkFollowed>20&&tkFollowBacks===0;
      // 今日数据标签辅助
      const _todayLabel=(n,color)=>`<div style="font-size:11px;font-weight:600;color:${color};line-height:1">${n>0?'+'+n:n}</div>`;
      kpiBarHtml=`<div id="tk-kpi-bar" style="display:flex;align-items:center;gap:12px;padding:11px 16px;background:linear-gradient(135deg,rgba(59,130,246,.07),rgba(139,92,246,.06));border:1px solid rgba(59,130,246,.18);border-radius:10px;margin-bottom:10px;flex-wrap:wrap">
        <div style="display:flex;flex-direction:column;gap:1px;flex-shrink:0">
          <span style="font-size:10px;font-weight:700;color:var(--text-dim);letter-spacing:.5px">📅 今日战报</span>
          <span style="font-size:9px;color:var(--text-muted)">累计/今日</span>
        </div>
        <div style="display:flex;gap:14px;flex:1;flex-wrap:wrap;align-items:center">
          <div style="text-align:center;min-width:48px">
            <div style="font-size:18px;font-weight:700;color:#60a5fa;line-height:1.1">${tkFollowed}</div>
            ${_todayLabel(tkFollowedToday,'#60a5fa')}
            <div style="font-size:9px;color:var(--text-muted);margin-top:1px">关注</div>
          </div>
          <div style="text-align:center;min-width:48px">
            <div style="font-size:18px;font-weight:700;color:${tkFollowBacks>0?'#22c55e':'#f87171'};line-height:1.1">${tkFollowBacks}</div>
            <div style="font-size:11px;color:${followBackRate>0?'#4ade80':'#94a3b8'};line-height:1">${followBackRate}%</div>
            <div style="font-size:9px;color:var(--text-muted);margin-top:1px">回关</div>
          </div>
          <div style="text-align:center;min-width:48px">
            <div style="font-size:18px;font-weight:700;color:#a78bfa;line-height:1.1">${tkDms}</div>
            ${_todayLabel(tkDmsToday,'#a78bfa')}
            <div style="font-size:9px;color:var(--text-muted);margin-top:1px">私信</div>
          </div>
          <div style="text-align:center;min-width:48px">
            <div style="font-size:18px;font-weight:700;color:${tkLeadsResp>0?'#22c55e':'#94a3b8'};line-height:1.1">${tkLeadsResp}</div>
            <div style="font-size:11px;color:${tkLeadsResp>0?'#4ade80':'#94a3b8'};line-height:1">${tkDms>0?(tkLeadsResp/tkDms*100).toFixed(0):0}%</div>
            <div style="font-size:9px;color:var(--text-muted);margin-top:1px">收到回复</div>
          </div>
          <div style="text-align:center;min-width:48px">
            <div style="font-size:18px;font-weight:700;color:${tkQualified>0?'#f97316':'#94a3b8'};line-height:1.1">${tkQualified}</div>
            <div style="font-size:11px;color:${tkQualified>0?'#fb923c':'#94a3b8'};line-height:1">${tkConverted>0?tkConverted+'✓':tkLeadsResp>0?(tkQualified/tkLeadsResp*100).toFixed(0)+'%':'—'}</div>
            <div style="font-size:9px;color:var(--text-muted);margin-top:1px">${tkQualified>0?'🔥 合格线索':'合格线索'}</div>
          </div>
          <div style="text-align:center;min-width:48px">
            <div style="font-size:18px;font-weight:700;color:#f97316;line-height:1.1">${tkWatchedToday}</div>
            <div style="font-size:11px;color:#fb923c;line-height:1">${tkLikedToday}♥</div>
            <div style="font-size:9px;color:var(--text-muted);margin-top:1px">今日刷视频</div>
          </div>
          <div style="text-align:center;min-width:54px">
            <div style="font-size:18px;font-weight:700;color:${tkRevTotal>0?'#22c55e':'#94a3b8'};line-height:1.1">${tkRevTotal>0?'€'+tkRevTotal.toFixed(0):'—'}</div>
            <div style="font-size:11px;color:${tkRevToday>0?'#4ade80':'#94a3b8'};line-height:1">${tkRevToday>0?'+€'+tkRevToday.toFixed(0):tkConvToday>0?tkConvToday+'单':'—'}</div>
            <div style="font-size:9px;color:var(--text-muted);margin-top:1px">成交营收</div>
          </div>
          <div style="text-align:center;min-width:42px"><div style="font-size:18px;font-weight:700;color:${totalRunning>0?'#f59e0b':'var(--text-dim)'};line-height:1.1">${totalRunning}</div><div style="font-size:11px;color:var(--text-muted);line-height:1">&nbsp;</div><div style="font-size:9px;color:var(--text-muted);margin-top:1px">执行中</div></div>
          ${followBackWarn?'<div style="display:flex;align-items:center;gap:5px;padding:3px 9px;background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);border-radius:6px;font-size:10px;color:#f87171;white-space:nowrap">⚠ 0回关率 — 账号权重需提升</div>':''}
          ${replyWarn?'<div style="display:flex;align-items:center;gap:5px;padding:3px 9px;background:rgba(234,179,8,.1);border:1px solid rgba(234,179,8,.3);border-radius:6px;font-size:10px;color:#fbbf24;white-space:nowrap">💡 0%回复 — 建议先互动再DM</div>':''}
        </div>
        <div style="display:flex;gap:6px;flex-shrink:0;align-items:center">
          <div id="tk-live-ticker" style="font-size:10px;color:#a78bfa;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:0;transition:opacity 1s"></div>
          <button class="qa-btn" style="font-size:10px;padding:3px 8px" onclick="loadPlatformPage('tiktok')">↺ 刷新</button>
          <button class="qa-btn" style="font-size:10px;padding:3px 8px" onclick="_tkExportReport()" title="导出今日战报HTML">📊</button>
        </div>
      </div>`;
      // 设备ban/critical告警横幅
      const critAlerts=(alertsData?.alerts||[]).filter(a=>
        a.level==='critical'||
        (a.level==='error'&&(a.message||'').length>3)
      );
      if(critAlerts.length){
        alertBannerHtml=critAlerts.slice(0,2).map(a=>{
          const alias=ALIAS[a.device_id]||a.device_id?.substring(0,8)||'设备';
          const ts=a.timestamp?new Date(a.timestamp).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}):'';
          return '<div style="display:flex;align-items:center;gap:10px;padding:9px 14px;background:rgba(239,68,68,.07);border:1px solid rgba(239,68,68,.3);border-radius:8px;margin-bottom:8px;font-size:12px">'+
            '<span style="font-size:16px;flex-shrink:0">🚨</span>'+
            '<div style="flex:1"><b style="color:#f87171">'+alias+'</b> — '+(a.message||'设备异常')+'</div>'+
            (ts?'<span style="font-size:10px;color:var(--text-muted);white-space:nowrap">'+ts+'</span>':'')+
            '<button class="qa-btn" style="font-size:10px;padding:2px 8px" onclick="navigateToPage(\'screen\')">查看</button>'+
          '</div>';
        }).join('');
      }
    }
    container.innerHTML=`
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <div style="display:flex;align-items:center;gap:12px">
          <span style="font-size:32px">${icon}</span>
          <div>
            <h3 style="font-size:20px;font-weight:700;margin:0">${info.name||platform}</h3>
            <div style="font-size:12px;color:var(--text-muted);margin-top:2px">${slogan}</div>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:8px;background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:6px 12px">
          <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${dc.online>0?'#22c55e':'#ef4444'};${dc.online>0?'box-shadow:0 0 6px #22c55e':''}"></span>
          <span style="font-size:12px;font-weight:500">${dc.online} / ${dc.total} 设备在线</span>
        </div>
      </div>
      ${kpiBarHtml}
      ${alertBannerHtml}
      <div id="plat-funnel-${platform}" style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:12px;display:${platform==='tiktok'?'block':'none'}">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
          <div style="font-size:13px;font-weight:600">📈 转化漏斗</div>
          <div style="font-size:10px;color:var(--text-dim)">点击各阶段查看详情</div>
        </div>
        <div id="funnel-chart" style="display:flex;flex-direction:column;gap:6px"></div>
        <div id="tk-ab-stats" style="margin-top:10px"></div>
      </div>
      ${platform==='tiktok'?`<div id="tk-qualified-card" style="background:var(--bg-card);border:1px solid rgba(249,115,22,.25);border-radius:10px;padding:14px;margin-bottom:12px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
          <div style="font-size:13px;font-weight:600">&#128293; 合格线索</div>
          <span style="font-size:10px;color:var(--text-dim)">回关 + 回复 = 高意向用户</span>
        </div>
        <div id="tk-qualified-list" style="font-size:12px;color:var(--text-muted)">加载中...</div>
      </div>
      <div id="tk-health-card" style="background:var(--bg-card);border:1px solid rgba(34,197,94,.2);border-radius:10px;padding:14px;margin-bottom:12px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
          <div style="font-size:13px;font-weight:600">&#129657; 账号健康</div>
          <span style="font-size:10px;color:var(--text-dim)">今日配额 · 关注 50/天 · 私信 80/天</span>
        </div>
        <div id="tk-health-panel" style="font-size:12px;color:var(--text-muted)">加载中...</div>
      </div>`:''}
      <div style="font-size:13px;font-weight:600;margin-bottom:10px">快捷操作</div>
      <div style="display:flex;flex-direction:column;gap:10px;margin-bottom:12px" id="tk-quick-phases">
        ${(PLAT_PHASES[platform]||[]).map(phase=>{
          const phaseTasks=tasks.filter(t=>phase.types.includes(t.type));
          if(!phaseTasks.length) return '';
          const phaseRunning=phase.types.reduce((a,tp)=>a+(_platRunningTasks[tp]||0),0);
          return `<div style="background:var(--bg-card);border:1px solid ${phase.highlight?phase.color+'44':'var(--border)'};border-radius:10px;padding:10px 12px;${phase.highlight?'box-shadow:0 0 0 1px '+phase.color+'22':''}">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
              <span style="font-size:11px;font-weight:700;color:${phase.color}">${phase.label}</span>
              <span style="font-size:10px;color:var(--text-dim)">${phase.desc}</span>
              ${phaseRunning>0?`<span style="margin-left:auto;font-size:10px;background:${phase.color}22;color:${phase.color};border-radius:4px;padding:1px 6px;font-weight:600">${phaseRunning}台运行中</span>`:''}
            </div>
            <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px">
              ${phaseTasks.map(t=>{
                const running=_platRunningTasks[t.type]||0;
                const tIcon=PLAT_TASK_ICONS[t.type]||icon;
                const tGrad=PLAT_TASK_GRADIENTS[t.type]||('linear-gradient(135deg,'+phase.color+','+phase.color+'dd)');
                const hint=PLAT_TASK_HINTS[t.type]||'';
                const isRunning=running>0;
                return `<div class="action-card${isRunning?' action-card-running':''}" onclick="_tkQuickTaskWithModal('${platform}','${t.type}')" style="position:relative;${isRunning?'border-color:'+phase.color+';':''}" title="${hint}">
                  <div class="action-icon" style="background:${tGrad}">${tIcon}</div>
                  <div class="action-label">${t.label}</div>
                  <div style="font-size:10px;color:var(--text-dim);margin-top:1px">${hint}</div>
                  <div class="action-desc">${isRunning?'<span style="color:#f59e0b;font-weight:600">&#128260; '+running+'台执行中</span>':'&#9675; 空闲'}</div>
                </div>`;
              }).join('')}
            </div>
          </div>`;
        }).join('')}
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:14px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
          <div style="font-size:13px;font-weight:600">📋 实战任务</div>
          <div style="display:flex;gap:6px">
            <button class="qa-btn" onclick="loadPlatformPage('${platform}')" style="padding:4px 10px;font-size:11px">↺ 刷新</button>
            <button class="qa-btn" onclick="navigateToPage('tasks')" style="padding:4px 10px;font-size:11px">全部 →</button>
          </div>
        </div>
        <div id="plat-recent-${platform}" style="font-size:12px;color:var(--text-muted)">加载中...</div>
      </div>`;
    _loadPlatRecentTasks(platform);
    if(platform==='tiktok'){
      _loadFunnel();
      _loadQualifiedLeads();
      _loadAbStats();
      _loadAccountHealth();
      _setupTkLiveTicker();
    }
    // 任务列表每10秒刷新
    if(window._platRefreshTimer) clearInterval(window._platRefreshTimer);
    window._platRefreshTimer=setInterval(()=>{if(_currentPage==='plat-'+platform){_fetchPlatTasks(platform).then(()=>_loadPlatRecentTasks(platform));}else{clearInterval(window._platRefreshTimer);}},10000);
    // 漏斗+合格线索每3分钟自动刷新
    if(platform==='tiktok'){
      if(window._tkFunnelTimer) clearInterval(window._tkFunnelTimer);
      window._tkFunnelTimer=setInterval(()=>{
        if(document.getElementById('funnel-chart')){_loadFunnel();_loadQualifiedLeads();_loadAccountHealth();}
        else clearInterval(window._tkFunnelTimer);
      },180000);
    }
  }catch(e){
    console.error('loadPlatformPage error:',platform,e);
    container.innerHTML=`<div style="text-align:center;padding:40px">
      <div style="font-size:32px;margin-bottom:12px">&#9888;</div>
      <div style="font-size:14px;font-weight:600;margin-bottom:8px">${platform.charAt(0).toUpperCase()+platform.slice(1)} 面板加载失败</div>
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:16px">${e.message||'未知错误'}</div>
      <button class="dev-btn" onclick="loadPlatformPage('${platform}')" style="padding:8px 18px">重试</button>
    </div>`;
  }
}
async function _loadPlatRecentTasks(platform){
  const el=document.getElementById('plat-recent-'+platform);
  if(!el) return;
  try{
    const arr=_platAllTasks.length?_platAllTasks:[];
    if(!arr.length){el.innerHTML='<div style="text-align:center;padding:16px;color:var(--text-dim);font-size:12px">暂无任务记录 · 点击上方快捷操作开始</div>';return;}
    // 从任务结果提取业务摘要
    function _getPlatOutcome(t){
      const r=t.result||{};
      if(t.type==='tiktok_check_inbox'){
        const ir=r.inbox_result||r;
        const nm=ir.new_messages??r.new_messages;
        if(nm>0) return {label:'🔔 '+nm+'新消息',color:'#22c55e'};
        const ck=ir.checked??r.checked_count;
        if(ck>0) return {label:'查'+ck+'条无新消息',color:'var(--text-dim)'};
      }
      if(t.type==='tiktok_follow'){
        const fr=r.follow_result||r;
        const fw=fr.followed??r.followed;
        if(fw>0) return {label:'+'+fw+'关注',color:'#22c55e'};
        if(fw===0) return {label:'0人(被过滤)',color:'#f59e0b'};
      }
      if(t.type==='tiktok_warmup'){
        const w=r.warmup_result?.watched??r.watched;
        if(w>0) return {label:'看'+w+'视频',color:'var(--text-dim)'};
      }
      if(t.type==='tiktok_check_and_chat_followbacks'){
        const m=r.messaged??r.result?.messaged;
        if(m>0) return {label:'发'+m+'条DM',color:'#22c55e'};
        if(m===0) return {label:'无新回关',color:'var(--text-dim)'};
      }
      if(t.status==='running'&&r.progress!=null) return {label:r.progress+'% '+(r.progress_msg||''),color:'var(--text-dim)'};
      if(t.status==='failed'&&r.error) return {label:r.error.substring(0,30),color:'#f87171'};
      return null;
    }
    // 批次分组: 同类型任务在5分钟内创建 = 同一批次
    function _groupBatches(tasks){
      const groups=[];
      const sorted=[...tasks].sort((a,b)=>(b.created_at||0)-(a.created_at||0));
      for(const t of sorted){
        const bucket=Math.floor((t.created_at||Date.now()/1000)/300);
        const last=groups[groups.length-1];
        if(last&&last.type===t.type&&last.bucket===bucket){
          last.items.push(t);
        } else {
          groups.push({type:t.type,bucket,items:[t],ts:t.created_at});
        }
      }
      return groups;
    }
    const sIcon={running:'🔄',completed:'✅',failed:'❌',pending:'⏳',cancelled:'🚫'};
    const sColor={running:'#f59e0b',completed:'#22c55e',failed:'#ef4444',pending:'#94a3b8',cancelled:'#64748b'};
    const sLabel={running:'执行中',completed:'完成',failed:'失败',pending:'等待',cancelled:'已取消'};
    const groups=_groupBatches(arr.slice(0,24));
    el.innerHTML=groups.slice(0,10).map((g,gi)=>{
      const cnt=g.items.length;
      const running=g.items.filter(t=>t.status==='running').length;
      const done=g.items.filter(t=>t.status==='completed').length;
      const failed=g.items.filter(t=>t.status==='failed').length;
      const pending=g.items.filter(t=>t.status==='pending').length;
      const batchSt=running>0?'running':done===cnt?'completed':failed>0?'failed':'pending';
      const name=TASK_NAMES[g.type]||g.type?.replace(platform+'_','')||'?';
      const tm=g.ts?new Date(typeof g.ts==='number'?g.ts*1000:g.ts).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}):'';
      // 合并所有已完成任务的业务摘要
      const outcomes=g.items.map(t=>_getPlatOutcome(t)).filter(Boolean);
      const firstOutcome=outcomes.find(o=>o.color==='#22c55e')||outcomes[0]||null;
      // 设备标签
      const devText=cnt===1?(ALIAS[g.items[0].device_id]||g.items[0].device_id?.substring(0,6)||'?'):(cnt+'台设备');
      // 多设备批次状态文字
      const stText=cnt>1
        ?[done>0?done+'完成':'',running>0?running+'执行中':'',pending>0?pending+'等待':'',failed>0?failed+'失败':''].filter(Boolean).join(' ')
        :(sLabel[batchSt]||batchSt);
      // 节点标签（_worker 字段，来自 Worker-03 的任务）
      const _wLabel=ip=>!ip?'主控':ip==='192.168.0.103'?'W03':('W'+ip.split('.').pop());
      const _workers=[...new Set(g.items.map(t=>t._worker).filter(Boolean))];
      const _allLocal=_workers.length===0;
      const nodeBadge=_allLocal?'':
        '<span style="font-size:9px;color:#60a5fa;background:rgba(96,165,250,.12);padding:1px 5px;border-radius:3px;margin-left:3px;font-weight:600">'+
        _workers.map(_wLabel).join('/')+'</span>';
      // 展开详情ID
      const expandId='plat-batch-'+gi;
      const expandable=cnt>1;
      const detailRows=expandable?g.items.map(t=>{
        const alias=ALIAS[t.device_id]||t.device_id?.substring(0,6)||'?';
        const out=_getPlatOutcome(t);
        const wl=t._worker?_wLabel(t._worker):'';
        const wBadge=wl?'<span style="font-size:9px;color:#60a5fa;margin-left:3px">['+wl+']</span>':'';
        return '<div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:11px;color:var(--text-dim)">'+
          '<span>'+sIcon[t.status]+'</span>'+
          '<span style="min-width:38px;font-weight:500">'+alias+wBadge+'</span>'+
          '<span style="flex:1">'+(out?'<span style="color:'+out.color+'">'+out.label+'</span>':(sLabel[t.status]||''))+'</span>'+
          '</div>';
      }).join(''):'';
      // 取消按钮：running/pending 批次才显示
      const canCancel=(batchSt==='running'||batchSt==='pending');
      const cancelableIds=g.items.filter(t=>t.status==='running'||t.status==='pending').map(t=>t.task_id);
      const cancelBtn=canCancel
        ?'<button onclick="event.stopPropagation();_cancelTaskBatch('+JSON.stringify(cancelableIds)+')" '
          +'style="padding:2px 8px;font-size:10px;background:rgba(239,68,68,.1);color:#ef4444;border:1px solid rgba(239,68,68,.3);border-radius:4px;cursor:pointer;white-space:nowrap;flex-shrink:0" '
          +'title="取消任务">✕ 取消</button>'
        :'';
      return '<div style="padding:8px 0;border-bottom:1px solid rgba(51,65,85,.4)"'+(expandable?' onclick="const e=document.getElementById(\''+expandId+'\');if(e)e.style.display=e.style.display===\'none\'?\'block\':\'none\';event.stopPropagation()"':'')+' style="cursor:'+(expandable?'pointer':'default')+'">'
        +'<div style="display:flex;align-items:center;gap:8px">'
          +'<span style="font-size:14px">'+sIcon[batchSt]+'</span>'
          +'<span style="font-weight:500;font-size:12px;white-space:nowrap">'+devText+nodeBadge+'</span>'
          +'<span style="flex:1;font-size:12px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+name+(firstOutcome?'<span style="font-size:10px;margin-left:8px;color:'+firstOutcome.color+'">'+firstOutcome.label+'</span>':'')+'</span>'
          +'<span style="color:'+sColor[batchSt]+';font-size:11px;font-weight:500;white-space:nowrap">'+stText+'</span>'
          +cancelBtn
          +'<span style="color:var(--text-dim);font-size:11px;min-width:34px;text-align:right">'+tm+'</span>'
          +(expandable?'<span style="font-size:9px;color:var(--text-dim)">▼</span>':'')
        +'</div>'
        +(expandable?'<div id="'+expandId+'" style="display:none;padding:4px 0 2px 22px">'+detailRows+'</div>':'')
      +'</div>';
    }).join('');
  }catch(e){el.innerHTML='<div style="color:var(--text-dim);font-size:12px">加载失败</div>';}
}
async function platformQuickTask(platform,taskType){
  const online=allDevices.filter(d=>d.status==='connected'||d.status==='online');
  if(!online.length){showToast('没有在线设备','warn');return;}
  const params={};
  if(taskType.includes('send_message')||taskType.includes('send_dm')){
    const msg=prompt('输入消息内容:');if(!msg) return;params.message=msg;
    const target=prompt('输入目标用户:');if(!target) return;
    params.username=target;params.target=target;params.contact=target;
  }
  try{
    const r=await api('POST',`/platforms/${platform}/batch`,{task_type:taskType,params});
    showToast(`已为 ${r.created||online.length} 台设备创建 ${TASK_NAMES[taskType]||taskType} 任务`);
    setTimeout(()=>loadPlatformPage(platform),1000);
  }catch(e){showToast('创建失败: '+(e.message||''),'warn');}
}
async function _platCustomExec(platform){
  const typeSel=document.getElementById(`plat-batch-type-${platform}`);
  const devSel=document.getElementById(`plat-device-${platform}`);
  const taskType=typeSel?.value;const did=devSel?.value;
  if(!taskType) return;
  try{
    if(did==='_all'){
      const r=await api('POST',`/platforms/${platform}/batch`,{task_type:taskType,params:{}});
      showToast(`已为 ${r.created||0} 台设备创建 ${TASK_NAMES[taskType]||taskType} 任务`);
    }else{
      await api('POST',`/platforms/${platform}/tasks`,{task_type:taskType,device_id:did,params:{}});
      showToast(`已为 ${ALIAS[did]||did.substring(0,8)} 创建 ${TASK_NAMES[taskType]||taskType} 任务`);
    }
    setTimeout(()=>loadPlatformPage(platform),1000);
  }catch(e){showToast('执行失败: '+(e.message||''),'warn');}
}
async function platformBatch(platform){
  const sel=document.getElementById(`plat-batch-type-${platform}`);
  const taskType=sel?.value;if(!taskType) return;
  try{
    const r=await api('POST',`/platforms/${platform}/batch`,{task_type:taskType,params:{}});
    showToast(`已创建 ${r.created||0} 个 ${TASK_NAMES[taskType]||taskType} 任务`);
    setTimeout(()=>loadPlatformPage(platform),1000);
  }catch(e){showToast('批量操作失败','warn');}
}
async function platformSingleTask(platform){
  const devSel=document.getElementById(`plat-device-${platform}`);
  const batchSel=document.getElementById(`plat-batch-type-${platform}`);
  const did=devSel?.value;const taskType=batchSel?.value;
  if(!did||!taskType) return;
  try{
    await api('POST',`/platforms/${platform}/tasks`,{task_type:taskType,device_id:did,params:{}});
    showToast(`已创建 ${TASK_NAMES[taskType]||taskType} 任务`);
    setTimeout(()=>loadPlatformPage(platform),1000);
  }catch(e){showToast('创建失败','warn');}
}

/* ═══════════════════════════════════════════════════════════
   VPN 二维码一键配置
   ═══════════════════════════════════════════════════════════ */

async function _vpnUploadQR(input){
  const file=input.files[0];
  if(!file) return;
  const result=document.getElementById('vpn-upload-result');
  result.style.display='block';
  result.innerHTML='<div style="color:var(--accent)">&#9203; 正在识别二维码并配置到所有设备...</div>';

  const formData=new FormData();
  formData.append('file',file);

  try{
    const token=localStorage.getItem('oc_token')||'';
    const r=await fetch(_apiOrigin+'/vpn/setup-from-qr',{
      method:'POST',
      headers:token?{'Authorization':'Bearer '+token}:{},
      body:formData
    });
    const d=await r.json();
    if(!r.ok){
      result.innerHTML=`<div style="color:var(--red)">&#10060; ${d.detail||'识别失败'}</div>`;
      return;
    }
    // 显示结果
    let html=`<div style="margin-bottom:8px"><b>协议:</b> ${d.protocol||'?'} &nbsp; <b>结果:</b> ${d.success}/${d.total} 成功</div>`;
    html+='<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:6px">';
    for(const dev of (d.results||[])){
      const color=dev.ok?'var(--green)':'var(--red)';
      const icon=dev.ok?'&#9989;':'&#10060;';
      const info=dev.ok?`IP: ${dev.ip||'?'} (${dev.country||'?'})`:(dev.error||'失败');
      html+=`<div style="font-size:11px;padding:6px 8px;background:var(--bg-input);border-radius:6px;border-left:3px solid ${color}">
        ${icon} <b>${dev.device_id?.substring(0,8)||'?'}</b> — ${info}
      </div>`;
    }
    html+='</div>';
    result.innerHTML=html;
    showToast(`VPN 配置完成: ${d.success}/${d.total} 台设备成功`,'success');
    // 更新状态徽章
    const badge=document.getElementById('vpn-status-badge');
    if(badge){
      badge.textContent=`${d.success}/${d.total} 已连接`;
      badge.style.background=d.success===d.total?'rgba(34,197,94,0.2)':'rgba(234,179,8,0.2)';
      badge.style.color=d.success===d.total?'var(--green)':'var(--yellow)';
    }
  }catch(e){
    result.innerHTML=`<div style="color:var(--red)">&#10060; ${e.message||'上传失败'}</div>`;
  }
  input.value='';
}

async function _vpnCheckAll(){
  showToast('正在检测所有设备 VPN 状态...');
  try{
    const d=await api('GET','/vpn/status');
    const devs=d.devices||[];
    const connected=devs.filter(v=>v.connected).length;
    const badge=document.getElementById('vpn-status-badge');
    if(badge){
      badge.textContent=`${connected}/${devs.length} 已连接`;
      badge.style.background=connected===devs.length?'rgba(34,197,94,0.2)':'rgba(234,179,8,0.2)';
      badge.style.color=connected===devs.length?'var(--green)':'var(--yellow)';
    }
    showToast(`VPN: ${connected}/${devs.length} 台设备已连接`);
  }catch(e){showToast('检测失败: '+e.message,'warn');}
}

async function _vpnShowHistory(){
  const panel=document.getElementById('vpn-history-panel');
  if(panel.style.display==='block'){panel.style.display='none';return;}
  panel.style.display='block';
  panel.innerHTML='<div style="color:var(--text-dim)">加载中...</div>';
  try{
    const d=await api('GET','/vpn/qr-history');
    const imgs=d.images||[];
    if(!imgs.length){panel.innerHTML='<div style="color:var(--text-dim);font-size:11px">暂无上传记录</div>';return;}
    panel.innerHTML='<div style="font-size:11px;font-weight:600;margin-bottom:6px">历史记录</div>'+
      imgs.map(img=>`<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid var(--border);font-size:11px">
        <div>${img.filename} <span style="color:var(--text-dim)">(${img.size_kb}KB)</span></div>
        <div style="color:var(--text-dim)">${img.time}</div>
        ${img.uri?`<button class="dev-btn" style="padding:2px 8px;font-size:10px" onclick="_vpnReapply('${img.uri.replace(/'/g,"\\'")}')">重新应用</button>`:''}
      </div>`).join('');
  }catch(e){panel.innerHTML='<div style="color:var(--red)">'+e.message+'</div>';}
}

async function _vpnReapply(uri){
  if(!confirm('将此 VPN 配置应用到所有设备？')) return;
  showToast('正在配置...');
  try{
    const d=await api('POST','/vpn/apply-uri',{uri});
    showToast(`VPN 配置: ${d.success}/${d.total} 台成功`,'success');
  }catch(e){showToast('失败: '+e.message,'warn');}
}

/* ═══════════════════════════════════════
   P3-A: 合格线索面板
   ═══════════════════════════════════════ */
function _tkTimeAgo(iso){
  if(!iso) return '';
  try{
    const diff=(Date.now()-new Date(iso).getTime())/1000;
    if(diff<60) return '刚刚';
    if(diff<3600) return Math.floor(diff/60)+'分钟前';
    if(diff<86400) return Math.floor(diff/3600)+'小时前';
    return Math.floor(diff/86400)+'天前';
  }catch(e){return '';}
}
async function _loadQualifiedLeads(){
  const el=document.getElementById('tk-qualified-list');
  if(!el) return;
  try{
    const d=await api('GET','/tiktok/qualified-leads?limit=15');
    const leads=d.leads||[];
    if(!leads.length){
      el.innerHTML='<div style="text-align:center;padding:14px;color:var(--text-dim);font-size:12px">暂无合格线索 · 等待用户回关并回复后自动升级 🌱</div>';
      return;
    }
    const rows=leads.map(lead=>{
      const lastIx=lead.recent_interactions?.[0];
      const preview=(lastIx?.content||'').replace(/</g,'&lt;').substring(0,40);
      const timeAgo=_tkTimeAgo(lead.updated_at);
      const sc=Math.round(lead.score||0);
      const scColor=sc>20?'#22c55e':sc>10?'#f59e0b':'#94a3b8';
      const uname=lead.username||lead.name||'Unknown';
      return `<div style="display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--border)">
        <div style="width:34px;height:34px;border-radius:50%;background:linear-gradient(135deg,#f97316,#ef4444);display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0">&#128100;</div>
        <div style="flex:1;min-width:0">
          <div style="font-size:12px;font-weight:600;color:var(--text)">@${uname}</div>
          <div style="font-size:10px;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${preview||'无最近互动'}</div>
        </div>
        <div style="text-align:right;flex-shrink:0;margin-right:4px">
          <div style="font-size:11px;font-weight:700;color:${scColor}">&#9733;${sc}</div>
          <div style="font-size:9px;color:var(--text-dim)">${timeAgo}</div>
        </div>
        <button class="qa-btn" style="font-size:10px;padding:3px 9px;flex-shrink:0;white-space:nowrap;color:#f97316;border-color:rgba(249,115,22,.3)" onclick="_tkFollowUpLead('${uname}')">跟进&#8594;</button>
      </div>`;
    }).join('');
    el.innerHTML=rows+
      `<div style="margin-top:10px;display:flex;gap:8px;justify-content:flex-end">
        <span style="font-size:10px;color:var(--text-dim);align-self:center">共 ${leads.length} 人待跟进</span>
        <button class="qa-btn" style="font-size:11px;padding:4px 12px" onclick="_tkBatchFollowUp()">&#128172; 批量跟进</button>
      </div>`;
  }catch(e){
    el.innerHTML='<div style="color:var(--text-muted);font-size:12px">加载失败: '+e.message+'</div>';
  }
}
function _tkFollowUpLead(username){
  api('POST','/tasks',{type:'tiktok_follow_up',params:{target_country:'italy',max_leads:1,target_language:'italian',_target_username:username}})
    .then(()=>{
      const btns=[...document.querySelectorAll('#tk-qualified-list .qa-btn')];
      const btn=btns.find(b=>b.textContent.includes('跟进'));
      if(btn){btn.textContent='&#10003; 已创建';btn.disabled=true;}
    }).catch(()=>{});
}
function _tkBatchFollowUp(){
  api('POST','/tasks',{type:'tiktok_follow_up',params:{target_country:'italy',max_leads:30,target_language:'italian'}})
    .then(()=>{
      const btn=document.querySelector('#tk-qualified-list .qa-btn:last-child');
      if(btn){btn.textContent='&#10003; 任务已创建';btn.disabled=true;}
    }).catch(()=>{});
}
function _tkExportReport(){window.open('/tiktok/daily-report/export','_blank');}

/* ═══════════════════════════════════════
   P3-C: A/B 话术效果看板
   ═══════════════════════════════════════ */
async function _loadAbStats(){
  const el=document.getElementById('tk-ab-stats');
  if(!el) return;
  try{
    const d=await api('GET','/tiktok/messages/ab-stats');
    const variants=d.variants||[];
    if(!variants.length){el.innerHTML='';return;}
    const ADAPT_MIN=30; // 自适应权重激活所需最低发送数
    const rows=variants.map(v=>{
      const sent=parseInt(v.sent)||0;
      const rr=parseFloat(v.reply_rate)||0;
      const rrColor=rr>15?'#22c55e':rr>5?'#f59e0b':'#f87171';
      const lastSent=v.last_sent?_tkTimeAgo(v.last_sent):'—';
      const adapted=sent>=ADAPT_MIN;
      // 自适应激活进度条
      const progPct=Math.min(100,Math.round(sent/ADAPT_MIN*100));
      const adaptBadge=adapted
        ?`<span style="font-size:8px;background:#22c55e22;color:#22c55e;padding:1px 5px;border-radius:4px;font-weight:600">自适应激活</span>`
        :`<span style="font-size:8px;color:var(--text-dim)">需再 ${ADAPT_MIN-sent} 条激活</span>`;
      const progressBar=!adapted?`
        <div style="height:3px;background:var(--border);border-radius:2px;margin-top:4px;overflow:hidden">
          <div style="height:100%;width:${progPct}%;background:linear-gradient(90deg,#3b82f6,#6366f1);border-radius:2px;transition:width 0.4s"></div>
        </div>`:
        `<div style="height:3px;background:#22c55e33;border-radius:2px;margin-top:4px"></div>`;
      // 权重倍率标签（仅已激活时显示）
      const weightLabel=adapted&&v.effective_weight
        ?`<div style="font-size:9px;color:${v.effective_weight>=1.5?'#f59e0b':'#94a3b8'}">×${parseFloat(v.effective_weight).toFixed(1)}</div>`:'';
      return `<div style="padding:7px 0;border-bottom:1px solid var(--border)">
        <div style="display:flex;align-items:flex-start;gap:8px">
          <div style="flex:1;min-width:0">
            <div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap">
              <span style="font-size:11px;font-weight:600;color:var(--text)">${v.name||v.id}</span>
              ${adaptBadge}
            </div>
            <div style="font-size:9px;color:var(--text-dim);margin-top:1px">${v.description||''}</div>
            ${progressBar}
          </div>
          <div style="display:flex;gap:8px;flex-shrink:0;align-items:center">
            <div style="text-align:center;min-width:32px">
              <div style="font-size:12px;font-weight:600;color:var(--text-dim)">${sent}</div>
              <div style="font-size:8px;color:var(--text-muted)">发送</div>
            </div>
            <div style="text-align:center;min-width:32px">
              <div style="font-size:12px;font-weight:600;color:#22c55e">${v.replied||0}</div>
              <div style="font-size:8px;color:var(--text-muted)">回复</div>
            </div>
            <div style="text-align:center;min-width:40px">
              <div style="font-size:13px;font-weight:700;color:${sent>0?rrColor:'var(--text-muted)'}">${sent>0?rr+'%':'—'}</div>
              <div style="font-size:8px;color:var(--text-muted)">回复率</div>
              ${weightLabel}
            </div>
            <div style="text-align:right;min-width:36px;font-size:8px;color:var(--text-dim)">${lastSent}</div>
          </div>
        </div>
      </div>`;
    }).join('');
    // 冠军标注
    const activeSent=variants.filter(v=>v.sent>=ADAPT_MIN);
    const champion=activeSent.length>=2
      ?activeSent.reduce((a,b)=>(parseFloat(a.reply_rate)||0)>=(parseFloat(b.reply_rate)||0)?a:b,activeSent[0])
      :null;
    const champNote=champion
      ?`<div style="font-size:9px;color:#f59e0b;margin-bottom:6px">&#127851; 领先变体: ${champion.name||champion.id} (${champion.reply_rate}%)</div>`
      :`<div style="font-size:9px;color:var(--text-dim);margin-bottom:6px">累计发送 ${variants.reduce((s,v)=>s+(v.sent||0),0)}/${ADAPT_MIN*variants.length} 次后激活自适应权重</div>`;
    el.innerHTML=`<div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border)">
      <div style="font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:4px">&#129514; 话术 A/B 测试（${variants.length} 个变体）</div>
      ${champNote}
      ${rows}
    </div>`;
  }catch(e){el.innerHTML='';}
}

/* ═══════════════════════════════════════
   P7-D: 实时 DM 活动 Ticker (SSE/WS 驱动)
   ═══════════════════════════════════════ */
function _setupTkLiveTicker(){
  // 防重复绑定
  if(window._tkLiveTickerBound) return;
  window._tkLiveTickerBound=true;
  const _show=(uname,msg)=>{
    const el=document.getElementById('tk-live-ticker');
    if(!el) return;
    const safe=(s)=>(s||'').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    el.innerHTML=`&#8594; ${safe(uname||'...')}`;
    el.title=safe(msg||'');
    el.style.opacity='1';
    setTimeout(()=>{if(el)el.style.opacity='0';},5000);
  };
  // 主路: WS CustomEvent（analytics.js 广播）
  window.addEventListener('ws-event',(e)=>{
    const {type,data}=e.detail||{};
    if(type==='tiktok.dm_sent'&&document.getElementById('tk-live-ticker')){
      _show(data?.username,data?.message);
    }
  });
  // 兜底: 轮询最近一条 tiktok.dm_sent 任务结果（每20s）
  window._tkTickerPoll=setInterval(async()=>{
    if(!document.getElementById('tk-live-ticker')){clearInterval(window._tkTickerPoll);return;}
    try{
      const tasks=await api('GET','/tasks?limit=5&status=completed');
      const arr=Array.isArray(tasks)?tasks:(tasks.tasks||[]);
      const dm=arr.find(t=>t.type==='tiktok_check_and_chat_followbacks'||t.type==='tiktok_send_dm');
      if(dm){
        let res={};try{res=JSON.parse(dm.result||'{}')}catch(e){}
        const users=(res.chat_result?.users||[]);
        if(users.length>0){
          const last=users[users.length-1];
          _show(last.name,last.message);
        }
      }
    }catch(e){}
  },20000);
}

/* ═══════════════════════════════════════
   P4-B: 账号健康面板
   ═══════════════════════════════════════ */
async function _loadAccountHealth(){
  const el=document.getElementById('tk-health-panel');
  if(!el) return;
  try{
    const d=await api('GET','/tiktok/account-health');
    const health=d.health||{};
    const entries=Object.entries(health);
    if(!entries.length){
      el.innerHTML='<div style="text-align:center;padding:10px;color:var(--text-dim);font-size:12px">暂无在线设备数据</div>';
      return;
    }
    const rows=entries.map(([did,h])=>{
      const score=h.health_score||0;
      const scoreColor=score>=70?'#22c55e':score>=40?'#f59e0b':'#f87171';
      const phase=h.phase||'unknown';
      const phaseColor={'cold_start':'#60a5fa','interest_building':'#a78bfa','active':'#22c55e','follow':'#3b82f6','chat':'#f59e0b'}[phase]||'#94a3b8';
      const phaseLabel={'cold_start':'冷启动','interest_building':'兴趣建立','active':'活跃','follow':'关注期','chat':'聊天期'}[phase]||phase;
      const dailyF=h.followed_today||0;
      const dailyD=h.dms_today||0;
      const followLimit=50;const dmLimit=80;
      const followPct=Math.min(100,Math.round(dailyF/followLimit*100));
      const dmPct=Math.min(100,Math.round(dailyD/dmLimit*100));
      const followBarColor=followPct>=90?'#f87171':followPct>=70?'#f59e0b':'#3b82f6';
      const dmBarColor=dmPct>=90?'#f87171':dmPct>=70?'#f59e0b':'#a78bfa';
      const uname=h.username?`@${h.username}`:(ALIAS[did]||did.substring(0,8));
      return `<div style="padding:10px 0;border-bottom:1px solid var(--border)">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
          <div style="width:32px;height:32px;border-radius:50%;background:linear-gradient(135deg,${scoreColor}44,${scoreColor}22);display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:700;color:${scoreColor};flex-shrink:0">${score}</div>
          <div style="flex:1;min-width:0">
            <div style="font-size:12px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${uname}</div>
            <span style="font-size:9px;font-weight:600;color:${phaseColor};background:${phaseColor}22;border-radius:4px;padding:1px 5px">${phaseLabel}</span>
          </div>
          <div style="text-align:right;flex-shrink:0">
            <div style="font-size:10px;color:var(--text-dim)">算法分 <b style="color:var(--text)">${(h.algorithm_score||0).toFixed(2)}</b></div>
            <div style="font-size:9px;color:var(--text-muted)">关注 ${h.total_followed||0} · DM ${h.total_dms_sent||0}</div>
          </div>
        </div>
        <div style="display:flex;gap:8px">
          <div style="flex:1">
            <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--text-muted);margin-bottom:2px"><span>今日关注</span><span style="color:${followBarColor}">${dailyF}/${followLimit}</span></div>
            <div style="height:4px;background:var(--border);border-radius:2px"><div style="height:100%;width:${followPct}%;background:${followBarColor};border-radius:2px;transition:width .3s"></div></div>
          </div>
          <div style="flex:1">
            <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--text-muted);margin-bottom:2px"><span>今日私信</span><span style="color:${dmBarColor}">${dailyD}/${dmLimit}</span></div>
            <div style="height:4px;background:var(--border);border-radius:2px"><div style="height:100%;width:${dmPct}%;background:${dmBarColor};border-radius:2px;transition:width .3s"></div></div>
          </div>
        </div>
      </div>`;
    }).join('');
    el.innerHTML=rows;
  }catch(e){
    el.innerHTML='<div style="color:var(--text-muted);font-size:12px">加载失败: '+e.message+'</div>';
  }
}

/* ═══════════════════════════════════════
   转化漏斗可视化
   ═══════════════════════════════════════ */
// TikTok漏斗阶段本地名称（防止API返回乱码）
const _TK_FUNNEL_NAMES=['视频观看','精准关注','回关检测','私信发送','收到回复','合格线索','转化成功'];
const _TK_FUNNEL_TIPS=[null,'关注率','回关率','发信率','回复率','资格率','转化率'];

async function _loadFunnel(){
  const el=document.getElementById('funnel-chart');
  if(!el) return;
  try{
    const d=await api('GET','/tiktok/funnel');
    const rawStages=d.stages||[];
    // 用本地名称覆盖（避免API乱码）
    const stages=rawStages.map((s,i)=>({...s,name:_TK_FUNNEL_NAMES[i]||s.name,tip:_TK_FUNNEL_TIPS[i]||''}));
    const maxVal=Math.max(1,...stages.map(s=>s.value));
    const followed=d.total_followed||0;
    const followBacks=d.follow_backs||0;
    const dms=d.total_dms||0;
    const replied=d.leads_responded||0;
    const followBackRate=followed>0?(followBacks/followed*100):0;
    // 基于位置的精准异常检测
    const anomalies=[];
    // 回关率: 关注了N人但0回关
    if(followed>20&&followBacks===0){
      anomalies.push({level:'error',msg:'⚠ 回关率 0% — 已关注'+followed+'人，无一回关。建议：①先养号互动提升权重 ②检查是否被限流'});
    } else if(followed>20&&followBackRate<5){
      anomalies.push({level:'warn',msg:'⚠ 回关率 '+followBackRate.toFixed(1)+'% 偏低 — 正常应达10-20%，建议增加视频互动'});
    }
    // 私信发送但无回复
    if(dms>10&&replied===0){
      anomalies.push({level:'error',msg:'💡 0%回复率 — '+dms+'条私信无回复。建议：先看视频+点赞互动，再发DM，回复率可提升5-15倍'});
    }
    // 通用阶段转化检测
    for(let i=1;i<stages.length;i++){
      const prev=stages[i-1].value;const cur=stages[i].value;
      // 跳过回关(i=2)已单独处理；跳过合格线索(i=5)为新指标，初期为0属正常
      if(prev>20&&cur===0&&i!==2&&i!==5){
        anomalies.push({level:'warn',msg:'⚠ '+stages[i].name+' = 0 — '+stages[i-1].name+'('+prev+')无一转化'});
      }
    }
    // qualified 提示：有回复但合格数为0（可能尚未产生双向互动）
    const qualifiedStage=stages[5];
    if(qualifiedStage&&stages[4]&&stages[4].value>5&&qualifiedStage.value===0){
      anomalies.push({level:'warn',msg:'💡 合格线索 0 — 已有'+stages[4].value+'人回复，等待他们也回关后自动升级为合格线索'});
    }
    el.innerHTML=stages.map((s,i)=>{
      const pct=Math.max(2,Math.round(s.value/maxVal*100));
      const prevVal=i>0?stages[i-1].value:0;
      const rate=prevVal>0?(s.value/prevVal*100).toFixed(1)+'%':'—';
      const rateColor=prevVal>0?(s.value/prevVal<0.03&&prevVal>10?'#ef4444':s.value/prevVal<0.15?'#f59e0b':'#22c55e'):'var(--text-dim)';
      const isAnomaly=prevVal>10&&s.value===0&&i>0;
      return '<div style="display:flex;align-items:center;gap:10px">'
        +'<div style="width:68px;text-align:right;font-size:11px;color:'+(isAnomaly?'#f87171':'var(--text-dim)')+';white-space:nowrap">'+s.name+'</div>'
        +'<div style="flex:1;position:relative;height:26px;background:var(--bg-input);border-radius:5px;overflow:hidden">'
          +'<div style="height:100%;width:'+pct+'%;background:'+(isAnomaly?'rgba(239,68,68,0.5)':s.color)+';border-radius:5px;transition:width .6s ease;display:flex;align-items:center;justify-content:flex-end;padding-right:8px">'
            +'<span style="color:#fff;font-size:11px;font-weight:700;text-shadow:0 1px 2px rgba(0,0,0,.5)">'+s.value+'</span>'
          +'</div>'
          +(s.value===0&&prevVal>10?'<div style="position:absolute;inset:0;display:flex;align-items:center;padding-left:8px;font-size:10px;color:#f87171">⚠ 0</div>':'')
        +'</div>'
        +'<div style="width:46px;text-align:center;font-size:10px;color:'+rateColor+';font-weight:'+(i>0&&prevVal>0?'600':'400')+'">'+( i>0?rate:'')+'</div>'
      +'</div>';
    }).join('');
    // 异常提醒 (优先显示在漏斗下方)
    if(anomalies.length){
      el.innerHTML+='<div style="margin-top:10px;display:flex;flex-direction:column;gap:6px">'+
        anomalies.map(a=>'<div style="padding:8px 12px;border-radius:6px;font-size:11px;background:'+(a.level==='error'?'rgba(239,68,68,.1)':'rgba(234,179,8,.1)')+';border-left:3px solid '+(a.level==='error'?'#ef4444':'#eab308')+'">'+a.msg+'</div>').join('')+
      '</div>';
    }
    // 汇总条
    const _qualified=d.leads_qualified||0;
    const _converted=d.leads_converted||0;
    el.innerHTML+='<div style="display:flex;gap:16px;margin-top:10px;padding-top:10px;border-top:1px solid var(--border);font-size:11px;flex-wrap:wrap">'
      +'<span style="color:var(--text-muted)">线索 <b style="color:var(--text)">'+(d.leads_total||0)+'</b></span>'
      +'<span style="color:var(--text-muted)">回关率 <b style="color:'+(followBackRate>10?'#22c55e':followBackRate>0?'#f59e0b':'#f87171')+'">'+followBackRate.toFixed(1)+'%</b></span>'
      +'<span style="color:var(--text-muted)">已回复 <b style="color:#22c55e">'+(d.leads_responded||0)+'</b></span>'
      +(_qualified>0?'<span style="color:var(--text-muted)">合格 <b style="color:#f97316;font-size:12px">🔥'+_qualified+'</b></span>':'<span style="color:var(--text-muted)">合格 <b style="color:var(--text-dim)">0</b></span>')
      +'<span style="color:var(--text-muted)">已转化 <b style="color:#ef4444">'+_converted+'</b></span>'
      +(_converted>0&&(d.leads_total||0)>0?'<span style="color:var(--text-muted)">转化率 <b style="color:#ef4444">'+(_converted/(d.leads_total||1)*100).toFixed(1)+'%</b></span>':'')
      +'<span style="color:var(--text-muted)">设备阶段 active:<b style="color:var(--text)">'+(d.device_phases?.active||0)+'</b> 建立中:<b style="color:var(--text)">'+(d.device_phases?.interest_building||0)+'</b></span>'
    +'</div>';
  }catch(e){
    el.innerHTML='<div style="color:var(--text-muted);font-size:12px">漏斗数据加载失败</div>';
  }
}

/* ── TikTok 快捷操作确认弹窗 ── */
async function _tkQuickTaskWithModal(platform, taskType){
  // 需要输入内容的任务，走原始逻辑
  if(taskType.includes('send_message')||taskType.includes('send_dm')){
    return platformQuickTask(platform, taskType);
  }
  let audienceHtml='';
  if(platform==='tiktok'&&PLAT_TK_AUDIENCE_TASKS.has(taskType)){
    try{
      const presets=await _loadAudiencePresetsCached();
      const opts=['<option value="">（不使用人群预设）</option>'].concat(
        presets.map(p=>{
          const id=String((p&&p.id)||'').replace(/"/g,'&quot;');
          const lab=String((p&&p.label)||(p&&p.id)||'').replace(/</g,'&lt;').replace(/"/g,'&quot;');
          return `<option value="${id}">${lab}</option>`;
        })
      );
      audienceHtml=`<div style="margin-bottom:14px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;gap:8px">
        <span style="font-size:13px;font-weight:600;color:var(--text-dim)">人群预设</span>
        <button type="button" onclick="_platTkRefreshAudienceSelect()" title="重新拉取服务器预设列表" style="font-size:11px;padding:4px 10px;border-radius:6px;border:1px solid var(--border);background:rgba(255,255,255,.06);color:var(--text-dim);cursor:pointer">↻ 刷新</button>
      </div>
      <select id="tkp-audience_preset" style="width:100%;padding:8px 10px;background:var(--bg-input);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px">${opts.join('')}</select>
      <div style="font-size:11px;color:var(--text-dim);margin-top:6px">与下方参数合并时，表单中的数值覆盖预设</div>
    </div>`;
    }catch(e){console.warn('[audience-presets]',e);}
  }
  const taskName=TASK_NAMES[taskType]||taskType;
  const hint=PLAT_TASK_HINTS[taskType]||'';
  const paramDefs=PLAT_TK_PARAMS[taskType]||[];
  const online=allDevices.filter(d=>d.status==='connected'||d.status==='online');
  const running=_platRunningTasks[taskType]||0;
  // 构建设备选择列表
  const devList=online.length?online.map(d=>{
    const alias=ALIAS[d.device_id]||d.device_id.substring(0,8);
    return `<label style="display:flex;align-items:center;gap:8px;padding:4px 0;cursor:pointer;font-size:13px"><input type="checkbox" data-did="${d.device_id}" checked style="accent-color:var(--accent);width:16px;height:16px"><span>${alias}</span></label>`;
  }).join(''):'<div style="color:var(--text-dim);font-size:13px">暂无在线设备</div>';
  // 构建参数输入列表
  const paramHtml=paramDefs.map(p=>{
    if(p.type==='checkbox') return `<label style="display:flex;align-items:center;gap:8px;font-size:13px"><input type="checkbox" id="tkp-${p.key}" ${p.default?'checked':''} style="accent-color:var(--accent);width:16px;height:16px"><span>${p.label}</span></label>`;
    if(p.type==='number') return `<div style="display:flex;align-items:center;gap:10px;font-size:13px"><span style="min-width:100px">${p.label}</span><input type="number" id="tkp-${p.key}" value="${p.default}" style="width:80px;padding:5px 8px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px"></div>`;
    if(p.type==='text') return `<div style="display:flex;align-items:center;gap:10px;font-size:13px"><span style="min-width:100px">${p.label}</span><input type="text" id="tkp-${p.key}" value="${p.default||''}" placeholder="${p.placeholder||p.label}" style="flex:1;padding:5px 10px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px"></div>`;
    if(p.type==='textarea') return `<div style="display:flex;flex-direction:column;gap:6px;font-size:13px"><span style="color:var(--text-dim)">${p.label}</span><textarea id="tkp-${p.key}" rows="5" placeholder="${p.placeholder||p.label}" style="padding:7px 10px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px;font-family:inherit;resize:vertical">${p.default||''}</textarea></div>`;
    if(p.type==='select'){
      const opts=(p.options||[]).map(opt=>{const[v,lbl]=Array.isArray(opt)?opt:[opt,opt];return `<option value="${v}" ${v===p.default?'selected':''}>${lbl}</option>`}).join('');
      return `<div style="display:flex;align-items:center;gap:10px;font-size:13px"><span style="min-width:100px">${p.label}</span><select id="tkp-${p.key}" style="flex:1;padding:6px 10px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px">${opts}</select></div>`;
    }
    if(p.type==='select_persona'){
      // 异步填充：默认空下拉，加载后替换
      return `<div style="display:flex;align-items:center;gap:10px;font-size:13px"><span style="min-width:100px">${p.label}</span><select id="tkp-${p.key}" data-select-persona="1" style="flex:1;padding:6px 10px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px"><option value="">加载中...</option></select></div>`;
    }
    return '';
  }).join('');
  const modal=document.createElement('div');
  modal.id='tk-task-modal';
  modal.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
  modal.innerHTML=`<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:16px;padding:24px;width:min(440px,95vw);box-shadow:0 20px 60px rgba(0,0,0,0.4)">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:18px">
      <div style="font-size:17px;font-weight:700">${PLAT_TASK_ICONS[taskType]||'▶'} ${taskName}</div>
      <button onclick="document.getElementById('tk-task-modal').remove()" style="background:rgba(255,255,255,.06);border:none;color:var(--text-muted);cursor:pointer;font-size:18px;width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center">✕</button>
    </div>
    ${running>0?`<div style="background:rgba(234,179,8,0.1);border:1px solid rgba(234,179,8,0.3);border-radius:8px;padding:10px 14px;margin-bottom:14px;font-size:13px;color:#f59e0b">⚠ 当前已有 ${running} 台设备在执行此任务</div>`:''}
    <div style="margin-bottom:14px">
      <div style="font-size:13px;font-weight:600;color:var(--text-dim);margin-bottom:8px">选择设备</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px">${devList}</div>
    </div>
    ${audienceHtml}
    ${paramDefs.length?`<div style="margin-bottom:14px">
      <div style="font-size:13px;font-weight:600;color:var(--text-dim);margin-bottom:10px">参数配置</div>
      <div style="display:flex;flex-direction:column;gap:10px">${paramHtml}</div>
    </div>`:''}
    ${hint?`<div style="font-size:12px;color:var(--text-dim);margin-bottom:14px">预计: ${hint}</div>`:''}
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="qa-btn" onclick="document.getElementById('tk-task-modal').remove()" style="padding:9px 18px;font-size:13px">取消</button>
      <button class="qa-btn" style="background:var(--accent);color:#fff;border-color:var(--accent);padding:9px 18px;font-size:13px;font-weight:600" onclick="_tkConfirmExec('${platform}','${taskType}')">▶ 立即执行</button>
    </div>
  </div>`;
  document.getElementById('tk-task-modal')?.remove();
  document.body.appendChild(modal);
  modal.addEventListener('click',e=>{if(e.target===modal)modal.remove();});
  // 异步填充画像下拉（profile_hunt 用）
  const personaSelects=modal.querySelectorAll('select[data-select-persona="1"]');
  if(personaSelects.length){
    api('GET','/facebook/target-personas').then(r=>{
      const personas=(r&&r.personas)||[];
      const html=['<option value="">默认 (config 指定)</option>']
        .concat(personas.map(p=>`<option value="${p.key}">${p.name||p.key}</option>`))
        .join('');
      personaSelects.forEach(sel=>{ sel.innerHTML=html; });
    }).catch(()=>{
      personaSelects.forEach(sel=>{ sel.innerHTML='<option value="">默认</option>'; });
    });
  }
}

async function _tkConfirmExec(platform, taskType){
  const modal=document.getElementById('tk-task-modal');
  // 收集选中的设备
  const checkedBoxes=modal?modal.querySelectorAll('input[data-did]:checked'):[];
  const deviceIds=[...checkedBoxes].map(el=>el.dataset.did);
  if(!deviceIds.length){showToast('请至少选择一台设备','warn');return;}
  // 收集参数
  const params={};
  const paramDefs=PLAT_TK_PARAMS[taskType]||[];
  paramDefs.forEach(p=>{
    const el=document.getElementById('tkp-'+p.key);
    if(!el) return;
    if(p.type==='checkbox') params[p.key]=el.checked;
    else if(p.type==='number') params[p.key]=parseFloat(el.value)||p.default;
    else if(p.type==='text') params[p.key]=el.value||p.default||'';
    else if(p.type==='textarea') params[p.key]=el.value||p.default||'';
    else if(p.type==='select'||p.type==='select_persona') params[p.key]=el.value||p.default||'';
  });
  const apEl=document.getElementById('tkp-audience_preset');
  if(apEl&&apEl.value) params.audience_preset=apEl.value;
  modal?.remove();
  try{
    let created=0;
    for(const did of deviceIds){
      await api('POST',`/platforms/${platform}/tasks`,{task_type:taskType,device_id:did,params});
      created++;
    }
    showToast(`已为 ${created} 台设备创建 ${TASK_NAMES[taskType]||taskType} 任务`,'success');
    setTimeout(()=>loadPlatformPage(platform),800);
  }catch(e){showToast('创建失败: '+(e.message||''),'error');}
}

async function _cancelTaskBatch(taskIds){
  if(!taskIds||!taskIds.length) return;
  const label=taskIds.length===1?'该任务':'这 '+taskIds.length+' 个任务';
  if(!confirm('确定要取消'+label+'吗？')) return;
  let ok=0,fail=0;
  for(const tid of taskIds){
    try{
      await api('POST','/tasks/'+tid+'/cancel');
      ok++;
    }catch(e){fail++;}
  }
  if(ok>0) showToast('已取消 '+ok+' 个任务'+(fail>0?' ('+fail+'失败)':''),'success');
  else showToast('取消失败','error');
  // 刷新当前平台任务列表
  const plat=_currentPage?.replace('plat-','');
  if(plat&&document.getElementById('plat-recent-'+plat)){
    await _fetchPlatTasks(plat);
    _loadPlatRecentTasks(plat);
  }
}

/* 供同页其他脚本复用（如需要异步拉取预设列表）；overview 流程已用静态 _TT_AUDIENCE_PARAM */
if(typeof window!=='undefined'){
  window._ocLoadAudiencePresetsCached=_loadAudiencePresetsCached;
  window.platTkNeedsAudienceModal=platTkNeedsAudienceModal;
  window._ocSeedAudiencePresetsCache=_ocSeedAudiencePresetsCache;
  window._ocReloadAndSeedAudiencePresets=_ocReloadAndSeedAudiencePresets;
  window._ocInvalidateAudiencePresetsCache=function(){
    _audiencePresetsCache=undefined;
    if(typeof window._ttFlowAudienceClientCacheReset==='function') window._ttFlowAudienceClientCacheReset();
  };
  window._platTkRefreshAudienceSelect=_platTkRefreshAudienceSelect;
}
