/* Novel Agent console — modernized UI logic. */
"use strict";

const CH = Object.freeze({
  PENDING:    {label:"排队中",    icon:"⏳", cls:"b-PENDING",    busy:true},
  PLANNING:   {label:"大纲设计中", icon:"🧭", cls:"b-PLANNING",   busy:true},
  GENERATING: {label:"正文撰写中", icon:"✍️", cls:"b-GENERATING", busy:true},
  REVIEWING:  {label:"审查中",    icon:"🔍", cls:"b-REVIEWING",  busy:true},
  DRAFT_READY:{label:"草稿就绪",  icon:"📄", cls:"b-DRAFT_READY",busy:false},
  WAITING_APPROVAL:{label:"待批准",icon:"⏳", cls:"b-WAITING_APPROVAL",busy:false},
  EXPORTED:   {label:"已导出",    icon:"📦", cls:"b-EXPORTED",   busy:false},
  PUBLISHED_MANUALLY:{label:"已发布",icon:"🚀",cls:"b-PUBLISHED_MANUALLY",busy:false},
  FAILED:     {label:"失败",      icon:"✖️", cls:"b-FAILED",     busy:false},
  CANCELLED:  {label:"已取消",    icon:"⊘",  cls:"b-CANCELLED",  busy:false}
});
const JB = Object.freeze({
  PENDING:   {label:"排队中", icon:"⏳", cls:"b-PENDING",  busy:true},
  RUNNING:   {label:"运行中", icon:"▶️", cls:"b-RUNNING",  busy:true},
  SUCCEEDED: {label:"成功",   icon:"✅", cls:"b-SUCCEEDED",busy:false},
  FAILED:    {label:"失败",   icon:"✖️", cls:"b-FAILED",   busy:false},
  CANCELLED: {label:"已取消", icon:"⊘",  cls:"b-CANCELLED",busy:false}
});
const STAGE_LABELS = {outline:"大纲设计", chapter:"正文撰写", polish:"润色"};
const STAGE_ICONS  = {outline:"🧭", chapter:"✍️", polish:"✨"};

const CODES = {
  missing_title:{t:"缺少章节标题",bad:1},
  missing_content:{t:"正文为空",bad:1},
  paragraph_format:{t:"段落格式不符合要求",bad:0},
  word_count_out_of_range:{t:"字数不在设定区间",bad:0},
  repeated_sentences:{t:"出现重复句子",bad:1},
  recent_chapter_overlap:{t:"与最近章节内容重复",bad:1},
  missing_chapter_goal:{t:"缺少本章目标",bad:1},
  goal_not_completed:{t:"本章目标未完成",bad:1},
  invalid_structured_output:{t:"模型输出无法解析为结构化章节",bad:1}
};
function codeMsg(c){
  c=String(c);
  if(c.indexOf(":")>=0){
    const k=c.split(":")[0], v=c.slice(k.length+1);
    if(k==="unauthorized_world_rule")return "使用了未登记的世界规则："+v;
    if(k==="timeline_event_redefinition")return "重定义既有时间线事件："+v;
    if(k==="foreshadowing_not_open")return "回收了尚未埋下的伏笔："+v;
    if(k==="forbidden_content")return "命中禁止内容："+v;
    if(k==="unregistered_character")return "出现未登记角色（建议补充）："+v;
  }
  return (CODES[c]&&CODES[c].t)||c;
}
function flattenError(text){
  if(!text)return text||"";
  const s=String(text).trim();
  try{const v=JSON.parse(s);if(Array.isArray(v))return v.map(codeMsg).join("；");}catch(_){}
  return s;
}
function explainError(text){
  if(!text)return null;
  const t=String(text);
  if(/DEEPSEEK_API_KEY is not configured/.test(t))
    return {kind:"config",title:"缺少 DeepSeek API Key",message:"服务端读不到 DEEPSEEK_API_KEY（当前 .env 中该值为空）。",fix:"1) 编辑项目根目录的 .env，填入真实密钥；2) 保存后重启 server 与 worker 两个进程。",hint:"DEEPSEEK_API_KEY 不入库，只从进程环境变量读取。"};
  if(/DEEPSEEK_API_KEY has surrounding whitespace/.test(t))
    return {kind:"config",title:"DEEPSEEK_API_KEY 首尾包含空格",fix:"去掉前后空格与引号后重启进程。"};
  if(/DEEPSEEK_BASE_URL is not configured/.test(t))
    return {kind:"config",title:"缺少 DEEPSEEK_BASE_URL",fix:"在 .env 中加入 DEEPSEEK_BASE_URL=https://api.deepseek.com 后重启进程。"};
  if(/DEEPSEEK_MODEL is not configured/.test(t))
    return {kind:"config",title:"缺少 DEEPSEEK_MODEL",fix:"在 .env 中设置 DEEPSEEK_MODEL 后重启进程。"};
  if(/DeepSeek HTTP 401|http_401/.test(t))
    return {kind:"config",title:"DeepSeek 鉴权失败（HTTP 401）",fix:"核对 .env 中的 DEEPSEEK_API_KEY 后重启进程。"};
  if(/DeepSeek HTTP 429|http_429/.test(t))
    return {kind:"http",title:"触发限流（HTTP 429）",fix:"稍等片刻再重试；若持续出现请检查账号余额/额度。"};
  if(/DeepSeek HTTP 400|http_400/.test(t))
    return {kind:"http",title:"请求被拒绝（HTTP 400）",fix:"尝试切换模型或检查配置后重试。"};
  if(/DeepSeek HTTP 404|http_404/.test(t))
    return {kind:"http",title:"接口或模型不存在（HTTP 404）",fix:"核对 DEEPSEEK_BASE_URL 与 DEEPSEEK_MODEL。"};
  if(/DNS/.test(t))return {kind:"http",title:"无法解析 DeepSeek 域名（DNS）",fix:"检查网络/代理后重试。"};
  if(/TLS/.test(t))return {kind:"http",title:"TLS 证书校验失败",fix:"若使用自定义代理/证书，请配置 DEEPSEEK_CA_BUNDLE。"};
  if(/TCP connection failure|connection timeout|first byte timeout/.test(t))
    return {kind:"http",title:"无法连接 DeepSeek（网络）",fix:"检查网络、防火墙或代理后重试。"};
  if(/timeout|timed out/.test(t))return {kind:"http",title:"请求超时",fix:"可调大 NOVEL_REQUEST_TIMEOUT 后重试。"};
  if(/stream interrupted|empty message\.content|missing message\.content/.test(t))
    return {kind:"other",title:"模型返回内容异常",fix:"点击「重试」再次生成。"};
  if(/output_truncated|max_tokens/.test(t))
    return {kind:"http",title:"输出被截断",fix:"可调大 NOVEL_MAX_CHAPTER_TOKENS 后重试。"};
  if(/novel_is_paused|novel_paused/.test(t))
    return {kind:"http",title:"小说当前处于暂停状态",fix:"点击「继续写作」恢复后重试。"};
  return null;
}
/* ---------------- helpers ---------------- */
function $(id){return document.getElementById(id);}
function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}
function fmtNum(n){return (n==null?0:n).toLocaleString("zh-CN");}
function timeAgo(iso){
  if(!iso)return"—";
  const d=new Date(iso); if(isNaN(d))return"—";
  const s=(Date.now()-d.getTime())/1000;
  if(s<0)return "刚刚"; if(s<60)return Math.floor(s)+" 秒前";
  if(s<3600)return Math.floor(s/60)+" 分钟前"; if(s<86400)return Math.floor(s/3600)+" 小时前";
  if(s<86400*30)return Math.floor(s/86400)+" 天前";
  return d.toLocaleDateString("zh-CN",{year:"numeric",month:"2-digit",day:"2-digit"});
}

/* ---------------- toast & banner ---------------- */
function toast(msg,kind){
  kind=kind||"info";
  const el=document.createElement("div");
  el.className="toast "+kind;
  const icons={ok:"✅",info:"ℹ️",warn:"⚠️",err:"❌"};
  el.innerHTML='<span>'+(icons[kind]||"ℹ️")+'</span><div class="t-msg"></div><button class="t-close">✕</button>';
  el.querySelector(".t-msg").textContent=msg;
  el.querySelector(".t-close").onclick=function(){el.remove();};
  $("toasts").appendChild(el);
  while($("toasts").children.length>5)$("toasts").firstChild.remove();
  setTimeout(function(){el.style.opacity="0";el.style.transition="opacity .3s";setTimeout(function(){el.remove();},320);},5600);
}
let bannerDismissed=false;
function setBanner(ex){
  const box=$("banner");
  if(!ex||bannerDismissed){box.hidden=true;box.innerHTML="";return;}
  const kind=ex.kind||"other";
  box.hidden=false; box.className="banner kind-"+kind;
  box.innerHTML='<div class="b-icon">'+(kind==="config"?"🔧":kind==="offline"?"📴":"⚠️")+'</div>'+
    '<div class="b-body"><div class="b-title">'+esc(ex.title)+'</div>'+
    (ex.message?'<div class="b-msg">'+esc(ex.message)+'</div>':'')+
    (ex.fix?'<div class="b-fix"><div style="font-weight:700;margin-bottom:6px">🔧 如何修复</div><code>'+esc(ex.fix).replace(/\n/g,"<br>")+'</code></div>':'')+
    (ex.hint?'<div style="margin-top:6px;font-size:12.5px;color:var(--faint)">'+esc(ex.hint)+'</div>':'')+
    '</div><button class="b-close" title="知道了">✕</button>';
  box.querySelector(".b-close").onclick=function(){bannerDismissed=true;box.hidden=true;};
}
function resetBanner(){bannerDismissed=false;}
function setConn(ok){
  const pill=$("connPill");
  if(ok){pill.innerHTML='<span class="dot"></span> 服务已连接';pill.classList.remove("bad");pill.classList.add("ok");}
  else{pill.innerHTML='<span class="dot off"></span> 无法连接';pill.classList.remove("ok");pill.classList.add("bad");}
}

/* ---------------- http ---------------- */
function token(){return (localStorage.getItem("novelAuthToken")||"").trim();}
async function request(u,o){
  o=o||{};
  const headers=Object.assign({},o.headers||{});
  const t=token();
  if(t&&!headers["Authorization"])headers["Authorization"]="Bearer "+t;
  if(o.body!==undefined&&!headers["Content-Type"])headers["Content-Type"]="application/json";
  const opts=Object.assign({},o);opts.headers=headers;
  let r;try{r=await fetch(u,opts);}catch(e){const er=new Error("无法连接本地服务端（"+e.message+"）");er.network=true;throw er;}
  let data=null;try{data=await r.json();}catch(_){}
  if(!r.ok){const e=new Error((data&&(data.message||data.code))||("HTTP "+r.status));e.status=r.status;e.data=data||{};throw e;}
  return {status:r.status,data:data};
}
async function api(u,o){return (await request(u,o)).data;}

/* ---------------- state ---------------- */
const state={
  novels:[],novel:null,chapters:[],jobs:[],usage:[],run:null,
  sig:"",active:false,busy:false,bibleDirty:false,selected:null,
  streaming:{active:false,chapter:0,chars:0},
  reveal:{chapter:0,full:null,shown:0},revealTimer:null,
  ev:{novelId:"",since:0,ctrl:null},refreshPending:false
};

/* ---------------- flow config ---------------- */
function flowMode(){
  const r=document.querySelector('input[name=flow]:checked');
  return r?r.value:"compose";
}
function flowLabel(){
  if(flowMode()==="compose")return "单次合成";
  if(flowMode()==="polish")return "大纲 → 正文 → 润色";
  return "大纲 → 正文";
}
function flowConfig(){
  const mode=flowMode();
  const stages = mode==="compose" ? [] : (mode==="polish" ? ["outline","chapter","polish"] : ["outline","chapter"]);
  const cfg={};
  if(stages.length)cfg.stages=stages;
  const tw=parseInt($("targetWords").value,10);
  if(tw>0)cfg.targetWords=tw;
  return Object.keys(cfg).length?cfg:null;
}
/* ---------------- live event stream (SSE) & typewriter ---------------- */
function eventAuthHeaders(){
  const h={};
  const t=token();
  if(t)h.Authorization="Bearer "+t;
  return h;
}
function stopEventStream(){
  if(state.ev.ctrl){state.ev.ctrl.abort();state.ev.ctrl=null;}
  state.ev.novelId="";
}
function ensureEventStream(){
  const nid=state.novel?state.novel.id:"";
  if(state.ev.ctrl&&state.ev.novelId!==nid)stopEventStream();
  if(!nid)return;
  if(state.ev.ctrl)return; // already connected
  state.ev.novelId=nid;
  const ctrl=new AbortController();
  state.ev.ctrl=ctrl;
  (async function(){
    try{
      const url="/api/events?novel_id="+encodeURIComponent(nid)+"&since="+(state.ev.since||0);
      const resp=await fetch(url,{headers:eventAuthHeaders(),signal:ctrl.signal});
      if(!resp.ok||!resp.body)throw new Error("events HTTP "+resp.status);
      const reader=resp.body.getReader();
      const dec=new TextDecoder();
      let buf="";
      for(;;){
        const {done,value}=await reader.read();
        if(done)break;
        buf+=dec.decode(value,{stream:true});
        let i;
        while((i=buf.indexOf("\n\n"))>=0){
          const frame=buf.slice(0,i);
          buf=buf.slice(i+2);
          if(frame.indexOf("data:")===0){
            const data=frame.slice(5).trim();
            if(data){try{handleEvent(JSON.parse(data));}catch(_){/* ignore bad frame */}}
          }
        }
      }
    }catch(e){
      // aborted (token/novel switch) or dropped connection — reopened by next refresh
    }finally{
      if(state.ev.ctrl===ctrl)state.ev.ctrl=null;
    }
  })();
}
function scheduleRefresh(){
  if(state.refreshPending)return;
  state.refreshPending=true;
  setTimeout(function(){state.refreshPending=false;refresh(true);},200);
}
function handleEvent(ev){
  if(!ev||!ev.id)return;
  state.ev.since=Math.max(state.ev.since||0,ev.id);
  const p=ev.payload||{};
  if(ev.type==="llm.delta"){
    state.streaming.active=true;
    state.streaming.chapter=p.chapter||state.streaming.chapter;
    state.streaming.chars+=(p.text||"").length;
  }else if(ev.type==="llm.text"){
    if(p.chapter&&p.text)startReveal(p.chapter,p.text);
  }else if(ev.type==="chapter.ready"){
    state.streaming.active=false;
    scheduleRefresh();
  }else if(ev.type==="agent.stage"||ev.type==="agent.run"||
           ev.type==="chapter.status"||ev.type==="checkpoint.saved"){
    scheduleRefresh();
  }
}
function revealActive(chapter){
  const r=state.reveal;
  return !!(r&&r.full!==null&&r.chapter===chapter&&r.shown<r.full.length);
}
function startReveal(chapter,text){
  if(state.reveal.full!==null&&state.reveal.chapter===chapter)return; // already typing
  state.reveal={chapter:chapter,full:text,shown:0};
  ensureRevealTicker();
  renderReader();
}
function ensureRevealTicker(){
  if(state.revealTimer)return;
  state.revealTimer=setInterval(revealTick,24);
}
function revealTick(){
  const r=state.reveal;
  if(!r||r.full==null){stopRevealTicker();return;}
  if(r.shown<r.full.length){
    r.shown=Math.min(r.full.length,r.shown+8);
    const el=$("liveProse");
    if(el)el.textContent=r.full.slice(0,r.shown);
    return;
  }
  state.reveal={chapter:0,full:null,shown:0};
  stopRevealTicker();
  renderReader(); // switch to normal paragraph layout
}
function stopRevealTicker(){
  if(state.revealTimer){clearInterval(state.revealTimer);state.revealTimer=null;}
}

/* ---------------- rendering ---------------- */
function hasActive(){
  return state.chapters.some(function(c){return CH[c.status]&&CH[c.status].busy;}) ||
         state.jobs.some(function(j){return JB[j.status]&&JB[j.status].busy;});
}
function signature(){
  const ns=state.novels.map(function(n){return [n.id,n.title,n.current_chapter,n.paused,n.story_bible_version].join("|");}).join(";");
  const cs=state.chapters.map(function(c){return [c.number,c.status,c.title||"",(c.content||"").length,(c.review&&c.review.score)||"",(c.review&&c.review.passed)?"1":"0",c.updated_at].join("|");}).join(";");
  const js=state.jobs.map(function(j){return [j.chapter_number,j.kind,j.status,j.attempts,j.updated_at].join("|");}).join(";");
  const rs=state.run?[state.run.status,state.run.current_stage||"",(state.run.stagesDetail||[]).map(function(s){return s.stage+s.state;}).join(",")].join("|"):"";
  return ns+"#"+cs+"#"+js+"#"+rs;
}
function renderAll(){
  renderNovelStats();
  renderChapterHint();
  renderChapterList();
  renderTimeline();
  renderJobs();
  renderUsage();
  renderBible();
  renderReader();
}
function renderNovelStats(){
  const box=$("novelStats");
  const n=state.novel;
  if(!n){box.innerHTML="";return;}
  const paused=!!n.paused;
  const parts=[];
  parts.push('<span class="chip">'+fmtNum(state.chapters.length)+' 章</span>');
  if(n.volume)parts.push('<span class="chip">'+esc(n.volume)+'</span>');
  if(n.genre)parts.push('<span class="chip">'+esc(n.genre)+'</span>');
  if(paused)parts.push('<span class="chip warn">⏸ 已暂停</span>');
  box.innerHTML=parts.join("");
  $("pause").hidden=!n||paused;
  $("resume").hidden=!n||!paused;
}
function renderChapterHint(){
  $("chapterHint").textContent=state.novel?("共 "+state.chapters.length+" 章"):"";
}
function renderChapterList(){
  const box=$("chapterList");
  if(!state.novel){box.innerHTML='<div class="empty">选择小说后显示章节</div>';return;}
  if(!state.chapters.length){box.innerHTML='<div class="empty">还没有章节<br><span style="font-size:12px;color:var(--faint)">点击「＋ 生成下一章」开始</span></div>';return;}
  box.innerHTML=state.chapters.slice().reverse().map(function(c){
    const m=CH[c.status]||{label:c.status,icon:"•",cls:"b-CANCELLED"};
    const title=c.title||(m.busy?"生成第 "+c.number+" 章…":"（未命名）");
    const active=state.selected===c.id?" active":"";
    return '<div class="ch-item'+active+'" onclick="openChapter(\''+esc(c.id)+'\')">'+
      '<span class="num">'+c.number+'</span>'+
      '<span class="t'+(c.title?"":" untitled")+'">'+esc(title)+'</span>'+
      badge(c.status,m)+'</div>';
  }).join("");
}
function badge(status,m){
  m=m||CH[status]||JB[status];
  const spin=m.busy?'<span class="spin"></span>':'';
  return '<span class="badge '+m.cls+'">'+spin+m.icon+' '+m.label+'</span>';
}
function renderTimeline(){
  const box=$("timeline");
  if(!state.novel){box.innerHTML='<div class="tl-empty">选择小说后显示 Agent 流水线</div>';return;}
  const run=state.run;
  const active=hasActive();
  if(!run || !run.stages || !run.stages.length){
    if(active){
      box.innerHTML='<div class="tl-item running"><div class="tl-node"><span class="spin"></span></div><div class="tl-body"><div class="tl-title">模型生成中</div><div class="tl-meta">当前模式：'+flowLabel()+'</div></div></div>';
    }else{
      box.innerHTML='<div class="tl-empty">尚未运行多智能体流水线。<br><span style="font-size:12px">当前模式：'+flowLabel()+'</span></div>';
    }
    return;
  }
  const detail={};
  (run.stagesDetail||[]).forEach(function(s){detail[s.stage]=s;});
  const runActive=(run.status==="RUNNING"||run.status==="PENDING");
  box.innerHTML=run.stages.map(function(st){
    const rec=detail[st];
    const stt=rec?rec.state:"PENDING";
    const cls = stt==="DONE"?"done":(stt==="RUNNING"?"running":(stt==="FAILED"?"failed":""));
    let icon = stt==="DONE"?"✓":(stt==="RUNNING"?'<span class="spin"></span>':(stt==="FAILED"?"✕":STAGE_ICONS[st]));
    let meta = rec&&rec.finished_at?("完成于 "+timeAgo(rec.finished_at)):(runActive?"进行中…":"等待中");
    if(stt==="FAILED"&&rec&&rec.error)meta="失败，将重试";
    return '<div class="tl-item '+cls+'"><div class="tl-node">'+icon+'</div><div class="tl-body"><div class="tl-title">'+STAGE_LABELS[st]+'</div><div class="tl-meta">'+meta+'</div></div></div>';
  }).join("");
}
function renderJobs(){
  const box=$("jobs");
  if(!state.novel){box.innerHTML='<div class="empty">选择小说后显示任务</div>';return;}
  if(!state.jobs.length){box.innerHTML='<div class="empty">暂无生成任务</div>';return;}
  box.innerHTML=state.jobs.slice(0,10).map(function(j){
    const m=JB[j.status]||{label:j.status,icon:"•",cls:"b-CANCELLED",busy:false};
    let err="";
    if(j.status==="FAILED"){const ex=explainError(j.error);err='<div class="j-err '+(ex&&ex.kind==="config"?"":"other")+'">'+esc(flattenError(j.error))+'</div>';}
    let actions="";
    if((j.status==="PENDING"||j.status==="RUNNING")&&j.kind==="generate")
      actions='<button class="btn xs" onclick="act(\'/api/jobs/'+esc(j.id)+'/cancel\',\'{}\',\'取消任务\',\'已取消\')">取消</button>';
    if(j.status==="FAILED"&&j.kind==="generate"&&j.novel_id)
      actions='<button class="btn xs danger" onclick="retryChapter(\''+esc(j.novel_id)+'\','+j.chapter_number+')">重试</button>';
    return '<div class="job-row"><div class="j-main">'+
      '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'+
      '<span class="badge '+m.cls+'">'+m.icon+' '+m.label+'</span>'+
      '<span>第 '+j.chapter_number+' 章 · <span class="j-kind">'+esc(j.kind)+'</span></span></div>'+
      (j.status==="FAILED"?err:'<div class="j-meta">第 '+j.attempts+' 次尝试 · '+esc(timeAgo(j.updated_at))+'</div>')+
      '</div>'+actions+'</div>';
  }).join("");
}
function renderUsage(){
  const box=$("usage");
  if(!state.novel){box.innerHTML='<div class="empty">暂无用量数据</div>';return;}
  if(!state.usage.length){box.innerHTML='<div class="empty">还没有调用记录</div>';return;}
  const list=state.usage;
  let tin=0,tout=0;list.forEach(function(x){tin+=x.input_tokens||0;tout+=x.output_tokens||0;});
  const ok=list.filter(function(x){return x.request_status==="succeeded";}).length;
  const failed=list.length-ok;
  box.innerHTML='<div class="usage-sum">'+
    '<div class="u-box"><div class="v">'+fmtNum(list.length)+'</div><div class="k">总调用</div></div>'+
    '<div class="u-box"><div class="v">'+fmtNum(tin)+'</div><div class="k">输入 tokens</div></div>'+
    '<div class="u-box"><div class="v">'+fmtNum(tout)+'</div><div class="k">输出 tokens</div></div>'+
    '<div class="u-box"><div class="v" style="color:'+(failed?'var(--bad)':'var(--ok)')+'">'+ok+(failed?'<span style="color:var(--bad)"> / '+failed+' 失败</span>':'')+'</div><div class="k">成功 / 失败</div></div>'+
    '</div>'+
    '<div class="tbl-wrap"><table class="tbl"><tr><th>时间</th><th>模型</th><th>输入</th><th>输出</th><th>耗时</th><th>状态</th></tr>'+
    list.slice(0,15).map(function(u){
      const st=u.request_status==="succeeded"?'<span style="color:var(--ok)">成功</span>':'<span style="color:var(--bad)">失败</span>';
      const err=u.request_status!=="succeeded"&&u.error?'<div style="color:var(--bad);font-size:11px">'+esc(flattenError(u.error))+'</div>':"";
      return '<tr><td>'+esc(timeAgo(u.created_at))+'</td><td>'+esc(u.model||"—")+'</td>'+
        '<td class="mono">'+fmtNum(u.input_tokens)+'</td><td class="mono">'+fmtNum(u.output_tokens)+'</td>'+
        '<td class="mono">'+(u.duration_ms?fmtNum(u.duration_ms)+"ms":"—")+'</td><td>'+st+err+'</td></tr>';
    }).join("")+'</table></div>';
}
function renderBible(){
  const n=state.novel;
  $("bibleVersion").textContent=n?("v"+n.story_bible_version+" · 更新于 "+timeAgo(n.updated_at)):"";
  $("saveBible").disabled=!n;
  if(n&&!state.bibleDirty&&document.activeElement!==$("bible")){
    $("bible").value=JSON.stringify(n.story_bible||{},null,2);
  }
  $("dirtyTag").classList.toggle("on",!!(state.bibleDirty&&n));
}
function renderProse(text){
  if(!text)return '<p style="color:var(--faint)">（本章暂无正文）</p>';
  const blocks=String(text).split(/\n\s*\n/);
  return blocks.map(function(b){
    const lines=b.split(/\n/).map(esc).join("<br>");
    return "<p>"+lines+"</p>";
  }).join("");
}
function chapterActions(c){
  const acts=[];
  const editable=["WAITING_APPROVAL","DRAFT_READY","EXPORTED","FAILED"];
  acts.push('<button class="btn xs ghost" title="查看本章草稿版本并回滚" onclick="openDraftHistory(\''+esc(c.id)+'\')">📑 历史</button>');
  acts.push('<button class="btn xs ghost" title="回放本章最近一次生成的事件与打字机效果" onclick="openRunReplay(\''+esc(c.id)+'\',\''+esc(c.novel_id)+'\','+c.number+')">🎞 回放</button>');
  if(c.content&&editable.indexOf(c.status)>=0&&c.status!=="PUBLISHED_MANUALLY")
    acts.push('<button class="btn xs" onclick="openEdit(\''+esc(c.id)+'\')">✏️ 编辑正文</button>');
  if(c.content&&c.status!=="PUBLISHED_MANUALLY"&&c.status!=="CANCELLED")
    acts.push('<button class="btn xs" onclick="act(\'/api/chapters/'+esc(c.id)+'/review\',\'{}\',\'重新审查\',\'重新审查完成\')">🔍 重新审查</button>');
  if(c.content&&(c.status==="WAITING_APPROVAL"||c.status==="DRAFT_READY")&&c.review&&c.review.passed)
    acts.push('<button class="btn xs success" onclick="act(\'/api/chapters/'+esc(c.id)+'/approve\',\'{}\',\'批准\',\'已批准\')">✅ 批准</button>');
  if(c.content&&(c.status==="WAITING_APPROVAL"||c.status==="DRAFT_READY"||c.status==="EXPORTED")&&c.review&&c.review.passed){
    acts.push('<button class="btn xs" onclick="exportChapter(\''+esc(c.id)+'\',\''+esc(c.novel_id)+'\','+c.number+',\'txt\')">TXT</button>');
    acts.push('<button class="btn xs" onclick="exportChapter(\''+esc(c.id)+'\',\''+esc(c.novel_id)+'\','+c.number+',\'md\')">MD</button>');
    acts.push('<button class="btn xs" onclick="exportChapter(\''+esc(c.id)+'\',\''+esc(c.novel_id)+'\','+c.number+',\'json\')">JSON</button>');
    acts.push('<button class="btn xs" onclick="exportChapter(\''+esc(c.id)+'\',\''+esc(c.novel_id)+'\','+c.number+',\'docx\')">DOCX</button>');
  }
  if(c.status==="EXPORTED")
    acts.push('<button class="btn xs" onclick="openPublish(\''+esc(c.id)+'\',\''+esc(c.novel_id)+'\','+c.number+')">🚀 已人工发布</button>');
  if(c.status==="FAILED"&&!c.content&&c.novel_id)
    acts.push('<button class="btn xs danger" onclick="retryChapter(\''+esc(c.novel_id)+'\','+c.number+')">↻ 重试生成</button>');
  const n=state.novel;
  const isBusy=!!(CH[c.status]&&CH[c.status].busy);
  if(n&&!isBusy&&c.number>(n.current_chapter||0))
    acts.push('<button class="btn xs danger" onclick="rewriteChapter(\''+esc(c.id)+'\')" title="删除本章全部内容并按 StoryBible 大纲重新生成">🗑 删除并重写</button>');
  return acts.join("");
}
function renderReader(){
  const box=$("reader");
  const c=state.chapters.find(function(x){return x.id===state.selected;});
  if(!c){
    box.innerHTML='<div class="empty" style="padding-top:80px"><div class="big">📖</div><p>选择左侧章节开始阅读，或点击「＋ 生成下一章」开始创作。</p></div>';
    return;
  }
  let fail="";
  if(c.status==="FAILED"){
    if(c.review&&c.review.blockingIssues&&c.review.blockingIssues.length)
      fail='<div class="ch-fail"><div class="t">⚠️ 自动审查未通过</div><ul class="probs">'+c.review.blockingIssues.map(function(x){return '<li class="p-bad">'+esc(codeMsg(x))+'</li>';}).join("")+'</ul></div>';
    else if(!c.content){
      const job=state.jobs.find(function(j){return j.chapter_number===c.number&&j.status==="FAILED";});
      fail='<div class="ch-fail"><div class="t">✖️ 本章生成失败</div><div>'+esc(job?flattenError(job.error):"")+'</div></div>';
    }
  }
  let reviewBox="";
  if(c.review&&(c.review.score!=null||(c.review.blockingIssues&&c.review.blockingIssues.length)||(c.review.issues&&c.review.issues.length)||(c.review.warnings&&c.review.warnings.length))){
    const passed=c.review.passed;
    const scoreCls=passed?"pass":"fail";
    const issues=(c.review.issues||[]).map(function(x){return '<li class="p-warn">'+esc(codeMsg(x))+'</li>';});
    const warnings=(c.review.warnings||[]).map(function(x){return '<li class="p-warn">'+esc(codeMsg(x))+'</li>';});
    const blocking=(c.review.blockingIssues||[]).map(function(x){return '<li class="p-bad">'+esc(codeMsg(x))+'</li>';});
    const list=blocking.concat(issues,warnings);
    reviewBox='<details class="review-box"><summary>🔎 查看审查结果（得分 '+esc(c.review.score)+(passed?" · 通过":" · 未通过")+'）</summary>'+
      '<div style="display:flex;gap:8px;align-items:center;margin:6px 0 4px">'+
      '<span class="score '+scoreCls+'">'+(passed?"✅ 通过":"❌ 未通过")+'</span>'+
      '<span class="score '+scoreCls+'">得分 '+esc(c.review.score)+'</span></div>'+
      (list.length?'<ul class="probs">'+list.join("")+'</ul>':'<div style="color:var(--ok);font-size:13px">无问题</div>')+
      '</details>';
  }
  let outline="";
  if(c.chapterGoal||(c.beats&&c.beats.length)||c.nextChapterHook){
    const beats=(c.beats||[]).map(function(b){return '<li>'+esc(typeof b==="string"?b:(b.goal||JSON.stringify(b)))+'</li>';}).join("");
    outline='<div class="outline-box"><h4>🧭 本章梗概</h4>'+
      (c.chapterGoal?'<div style="margin-bottom:6px"><b>目标：</b>'+esc(c.chapterGoal)+'</div>':'')+
      (beats?'<ul class="probs">'+beats+'</ul>':'')+
      (c.nextChapterHook?'<div style="margin-top:6px"><b>下章钩子：</b>'+esc(c.nextChapterHook)+'</div>':'')+
      '</div>';
  }
  const meta=c.content
    ? '正文约 '+fmtNum(c.content.length)+' 字 · '+(c.model?esc(c.model):"")+' · '+esc(timeAgo(c.generated_at||c.updated_at))
    : esc(timeAgo(c.updated_at));
  let bodyHtml;
  if(revealActive(c.number)){
    const r=state.reveal;
    bodyHtml='<div class="reader"><div class="live-prose" id="liveProse">'+esc(r.full.slice(0,r.shown))+'</div><span class="caret"></span></div>';
  }else if((CH[c.status]&&CH[c.status].busy)&&!c.content){
    const label=(CH[c.status]&&CH[c.status].label)||c.status;
    bodyHtml='<div class="reader"><p class="genhint"><span class="spin"></span> '+label+'… 已接收 '+fmtNum(state.streaming.chars)+' 字</p></div>';
  }else{
    bodyHtml='<div class="reader">'+renderProse(c.content)+'</div>';
  }
  box.innerHTML='<div class="reader-head">'+
    '<div class="num">第 '+c.number+' 章</div>'+
    '<h2>'+esc(c.title||"（未命名）")+'</h2>'+
    '<div class="reader-meta">'+meta+'</div>'+
    '<div class="reader-actions">'+chapterActions(c)+'</div>'+
    '</div>'+
    fail+outline+reviewBox+
    bodyHtml;
}
/* ---------------- actions ---------------- */
async function act(url,body,doing,done){
  try{
    toast((doing||"提交")+"…","info");
    await api(url,{method:"POST",body:body||"{}"});
    toast(done||"操作成功","ok");
    await refresh(true);
  }catch(e){
    const ex=explainError(e.message);
    if(ex&&ex.kind==="config"){setBanner(ex);toast(ex.title,"err");return;}
    toast("操作失败："+e.message,"err");
  }
}
function errToast(e,prefix){
  const ex=explainError(e.message);
  if(ex&&ex.kind==="config"){setBanner(ex);toast(ex.title,"err");return;}
  toast((prefix||"操作失败：")+e.message,"err");
}
function toggleNew(){const p=$("newPanel");p.hidden=!p.hidden;if(!p.hidden)$("newTitle").focus();}
function closeNew(){$("newPanel").hidden=true;}
function openChapter(cid){
  state.selected=cid;
  renderChapterList();
  renderReader();
}
async function generateNext(){
  const n=state.novel;
  if(!n)return toast("请先选择或新建一本小说","warn");
  if(n.paused)return toast("小说已暂停，请先点「继续写作」","warn");
  if(state.busy)return;
  const nextNum=(n.current_chapter||0)+1;
  const blk=state.chapters.find(function(c){return c.number===nextNum;});
  if(blk&&blk.content&&["WAITING_APPROVAL","DRAFT_READY","EXPORTED"].indexOf(blk.status)>=0)
    return toast("第 "+nextNum+" 章尚未完成「批准 → 导出 → 已人工发布」确认，请先完成后再生成下一章。","warn");
  const cfg=flowConfig();
  const body=cfg?JSON.stringify({config:cfg}):"{}";
  try{
    toast("已提交生成任务（"+flowLabel()+"）…","info");
    await api("/api/novels/"+n.id+"/chapters/generate",{method:"POST",body:body});
    toast("已排队生成第 "+nextNum+" 章","ok");
    await refresh(true);
    const nc=state.chapters.find(function(x){return x.number===nextNum;});
    if(nc){state.selected=nc.id;renderChapterList();renderReader();}
  }catch(e){errToast(e);}
}
async function retryChapter(nid,number){
  try{
    await api("/api/novels/"+nid+"/chapters/generate",{method:"POST",body:JSON.stringify({chapterNumber:number})});
    toast("已重新排队生成第 "+number+" 章","ok");
    await refresh(true);
  }catch(e){errToast(e);}
}
async function rewriteChapter(cid){
  if(!confirm("确定删除本章全部内容并按 StoryBible 大纲重新生成吗？此操作不可撤销。"))return;
  try{
    await api("/api/chapters/"+cid+"/rewrite",{method:"POST",body:"{}"});
    toast("已删除并重新排队生成","ok");
    await refresh(true);
  }catch(e){errToast(e);}
}
async function exportChapter(cid,nid,number,fmt){
  try{
    const x=await api("/api/chapters/"+cid+"/export",{method:"POST",body:JSON.stringify({novelId:nid,chapterNumber:number,format:fmt})});
    const fmtLabel={txt:"TXT",md:"Markdown",json:"JSON",docx:"DOCX"}[fmt]||fmt;
    toast("已导出 "+fmtLabel+" → "+x.path,"ok");
    await refresh(true);
  }catch(e){errToast(e,"导出失败：");}
}
function openEdit(cid){
  const c=state.chapters.find(function(x){return x.id===cid;});
  if(!c)return;
  openModal("✏️ 编辑正文（第 "+c.number+" 章）",
    '<label class="f">章节标题<input type="text" id="mTitle" value="'+esc(c.title||"")+'"></label>'+
    '<label class="f">正文（保存后自动进入待审查）<textarea id="mContent" rows="14" style="min-height:320px">'+esc(c.content||"")+'</textarea></label>',
    [{label:"保存",primary:true,fn:function(){submitEdit(cid);}},
     {label:"取消",primary:false,fn:closeModal}]);
}
async function submitEdit(cid){
  const body={title:$("mTitle").value,content:$("mContent").value};
  try{
    await api("/api/chapters/"+cid,{method:"PATCH",body:JSON.stringify(body)});
    toast("草稿已保存，请重新审查","ok");
    closeModal();await refresh(true);
  }catch(e){errToast(e,"保存失败：");}
}
function openPublish(cid,nid,number){
  openModal("🚀 人工发布确认（第 "+number+" 章）",
    '<p style="margin:0 0 12px;font-size:13.5px;color:var(--muted)">请先在目标平台（番茄小说等）手动上传本章导出稿，发布成功后再回来确认。</p>'+
    '<label class="f">发布平台（必填）<input type="text" id="pPlatform" placeholder="例如：番茄小说"></label>'+
    '<label class="f">操作人（必填）<input type="text" id="pOperator" value="local-user"></label>'+
    '<label class="f">外部链接（可选）<input type="text" id="pUrl" placeholder="https://…"></label>'+
    '<label class="f">备注（可选）<input type="text" id="pNotes"></label>',
    [{label:"确认已发布",primary:true,fn:function(){submitPublish(cid,nid,number);}},
     {label:"取消",primary:false,fn:closeModal}]);
}
async function submitPublish(cid,nid,number){
  const platform=$("pPlatform").value.trim();
  const operator=$("pOperator").value.trim();
  if(!platform||!operator)return toast("平台与操作人必填","warn");
  const body={novelId:nid,chapterNumber:number,platform:platform,operator:operator,publishedAt:new Date().toISOString()};
  if($("pUrl").value.trim())body.externalUrl=$("pUrl").value.trim();
  if($("pNotes").value.trim())body.notes=$("pNotes").value.trim();
  try{
    await api("/api/chapters/"+cid+"/publish",{method:"POST",body:JSON.stringify(body)});
    toast("已确认第 "+number+" 章发布完成","ok");
    closeModal();await refresh(true);
  }catch(e){errToast(e,"发布确认失败：");}
}
/* ---------------- draft history & run replay ---------------- */
function sleep(ms){return new Promise(function(r){setTimeout(r,ms);});}
let dhCtx=null;
async function openDraftHistory(cid){
  const c=state.chapters.find(function(x){return x.id===cid;});
  if(!c)return;
  let data;
  try{data=await api("/api/chapters/"+cid+"/history");}
  catch(e){errToast(e);return;}
  const versions=data.versions||[];
  if(!versions.length){toast("该章节还没有历史草稿版本","warn");return;}
  dhCtx={cid:cid,c:c,versions:versions};
  renderDraftList();
}
function renderDraftList(){
  const vs=dhCtx.versions.slice().reverse();
  const rows=vs.map(function(v,i){
    const origin=(v.passed===true||v.passed===false)?"模型生成":"手动编辑/回滚";
    const review=v.passed===true?'<span class="badge b-WAITING_APPROVAL">✅ 审查通过</span>':
      (v.passed===false?'<span class="badge b-FAILED">审查未过</span>':'<span class="badge b-PENDING">未审查</span>');
    return '<div class="hist-row"><div class="hist-meta">'+
      '<b>v'+v.version+'</b> · '+esc(timeAgo(v.createdAt))+' · '+esc(origin)+' '+review+
      '<div class="hist-title">'+esc(v.title||"（未命名）")+'</div></div>'+
      '<div class="hist-actions">'+
      '<button class="btn xs" onclick="showDraftPreview('+i+')">预览</button>'+
      '<button class="btn xs" onclick="diffDraft('+i+')">与当前对比</button>'+
      '<button class="btn xs danger" onclick="rollbackDraft('+i+')">回滚到此版</button>'+
      '</div></div>';
  }).join("");
  const lock=dhCtx.c.status==="PUBLISHED_MANUALLY"
    ? '<div style="color:var(--bad);margin-bottom:10px">当前章节已人工发布，正文不可回滚。</div>' : "";
  openModal("📑 草稿版本历史（第 "+dhCtx.c.number+" 章）",
    '<div class="hist-note">'+lock+'每一版草稿都留档于此；回滚会生成一个新版本，不会清空历史，且需重新审查。</div>'+
    '<div class="hist-list">'+rows+'</div>',
    [{label:"关闭",primary:false,fn:closeModal}]);
}
function showDraftPreview(i){
  const v=dhCtx.versions[i];
  openModal("📖 预览 v"+v.version,
    '<div class="modal-read"><h3 style="margin-top:0">'+esc(v.title||"（未命名）")+'</h3>'+renderProse(v.content||"（该版本无正文）")+'</div>',
    [{label:"返回列表",primary:false,fn:renderDraftList},{label:"关闭",primary:false,fn:closeModal}]);
}
function diffHtml(oldText,newText){
  const a=String(oldText||"").split("\n"), b=String(newText||"").split("\n");
  const n=a.length,m=b.length;
  const dp=Array.from({length:n+1},function(){return new Array(m+1).fill(0);});
  for(let i=n-1;i>=0;i--)for(let j=m-1;j>=0;j--)
    dp[i][j]=a[i]===b[j]?dp[i+1][j+1]+1:Math.max(dp[i+1][j],dp[i][j+1]);
  let i=0,j=0,out=[];
  while(i<n&&j<m){
    if(a[i]===b[j]){out.push('<div class="dl same">'+esc(a[i]||" ")+'</div>');i++;j++;}
    else if(dp[i+1][j]>=dp[i][j+1]){out.push('<div class="dl rem">− '+esc(a[i]||" ")+'</div>');i++;}
    else{out.push('<div class="dl add">+ '+esc(b[j]||" ")+'</div>');j++;}
  }
  while(i<n){out.push('<div class="dl rem">− '+esc(a[i])+'</div>');i++;}
  while(j<m){out.push('<div class="dl add">+ '+esc(b[j])+'</div>');j++;}
  return out.join("");
}
function diffDraft(i){
  const v=dhCtx.versions[i];
  openModal("🔍 对比：当前草稿 vs v"+v.version,
    '<div class="dl-legend"><span class="dl-tag rem">红 · 仅当前草稿</span><span class="dl-tag add">绿 · 仅所选版本 v'+v.version+'</span></div>'+
    '<div class="dl-box">'+diffHtml(dhCtx.c.content||"",v.content||"")+'</div>',
    [{label:"返回列表",primary:false,fn:renderDraftList},{label:"关闭",primary:false,fn:closeModal}]);
}
async function rollbackDraft(i){
  const v=dhCtx.versions[i];
  if(dhCtx.c.status==="PUBLISHED_MANUALLY"){toast("已发布章节不可回滚","warn");return;}
  if(!confirm("确定回滚到 v"+v.version+" 吗？将生成新版本并需重新审查。"))return;
  try{
    await api("/api/chapters/"+dhCtx.cid+"/rollback",{method:"POST",body:JSON.stringify({version:v.version})});
    toast("已回滚到 v"+v.version,"ok");
    closeModal();await refresh(true);
  }catch(e){errToast(e,"回滚失败：");}
}
let replayCtx=null;
async function openRunReplay(cid,nid,number){
  if(replayCtx&&replayCtx.running)replayCtx.stop=true;
  let data;
  try{data=await api("/api/novels/"+nid+"/chapters/"+number+"/timeline");}
  catch(e){errToast(e);return;}
  const events=data.events||[];
  if(!events.length){toast("该章节还没有可回放的事件记录（生成期间写入 events 表）","warn");return;}
  replayCtx={cid:cid,events:events,index:0,running:false,stop:false,paused:false};
  openModal("🎞 回放第 "+number+" 章生成过程（"+events.length+" 条事件）",
    '<div class="replay-stage" id="rpStage"></div>'+
    '<div class="replay-text" id="rpText"><span style="color:var(--faint)">点击「▶ 开始回放」查看。</span></div>'+
    '<div class="replay-log" id="rpLog"></div>',
    [{label:"▶ 开始",primary:true,fn:startReplay},
     {label:"⏸ 暂停/继续",primary:false,fn:toggleReplayPause},
     {label:"关闭",primary:false,fn:closeReplay}]);
  $("rpStage").textContent="共 "+events.length+" 条事件，按 SSE 原序重现：";
}
function closeReplay(){if(replayCtx)replayCtx.stop=true;closeModal();}
function toggleReplayPause(){
  if(!replayCtx||!replayCtx.running)return toast("请先开始回放","warn");
  replayCtx.paused=!replayCtx.paused;
  toast(replayCtx.paused?"已暂停":"已继续","info");
}
function replayLine(e){
  const p=e.payload||{};
  const st=p.stage?((STAGE_LABELS[p.stage]||p.stage)+" "):"";
  const text=String(p.text||"");
  let line="";
  if(e.type==="agent.stage")line="🛠 节点 "+st+(p.state==="running"?"开始":p.state==="done"?"完成":p.state);
  else if(e.type==="agent.run")line="🧠 流水线状态 → "+(p.status==="SUCCEEDED"?"成功":p.status);
  else if(e.type==="chapter.status")line="📌 章节状态 → "+((CH[p.status]&&CH[p.status].label)||p.status);
  else if(e.type==="checkpoint.saved")line="💾 检查点已保存（"+st+"）";
  else if(e.type==="llm.delta")line="✍️ "+st+"增量 "+text.length+" 字";
  else if(e.type==="llm.text")line="📃 校验通过，正文 "+text.length+" 字开始揭示";
  else if(e.type==="chapter.ready")line="🏁 完成（"+((CH[p.status]&&CH[p.status].label)||p.status)+"）";
  else line=e.type;
  const div=document.createElement("div");
  div.className="rp-line";div.textContent="#"+e.id+" "+line;
  const log=$("rpLog");
  if(!log)return;
  log.appendChild(div);log.scrollTop=log.scrollHeight;
}
async function startReplay(){
  if(!replayCtx||replayCtx.running)return;
  replayCtx.running=true;replayCtx.stop=false;replayCtx.paused=false;
  const text=$("rpText");
  if(text)text.textContent="";
  for(let k=0;k<replayCtx.events.length&&!replayCtx.stop;k++){
    while(replayCtx.paused&&!replayCtx.stop)await sleep(120);
    if(replayCtx.stop||!document.getElementById("rpLog")){replayCtx.stop=true;break;}
    const e=replayCtx.events[k];replayCtx.index=k;
    replayLine(e);
    if(e.type==="llm.text"){
      const full=String((e.payload&&e.payload.text)||"");
      if(text){text.textContent="";for(let ch=0;ch<full.length&&!replayCtx.stop;ch++){text.textContent+=full[ch];if(ch%5===0)await sleep(4);}}
    }else if(e.type==="llm.delta"){
      if(text&&text.textContent.indexOf("点击")<0)text.textContent="▍正在实时接收增量…";
    }
    await sleep(60);
  }
  replayCtx.running=false;
  if(text&&!replayCtx.stop)text.textContent+="\n✔ 回放完成";
}
/* ---------------- modal ---------------- */
function openModal(title,body,footBtns){
  const t=$("modalTitle");
  t.innerHTML=esc(title)+'<span class="spacer"></span><button class="btn ghost sm" id="modalClose">✕</button>';
  $("modalClose").onclick=closeModal;
  $("modalBody").innerHTML=body;
  const foot=$("modalFoot");foot.innerHTML="";
  (footBtns||[]).forEach(function(b){
    const el=document.createElement("button");
    el.className="btn sm"+(b.primary?" primary":"");
    el.textContent=b.label;
    el.onclick=b.fn;
    foot.appendChild(el);
  });
  $("modal").hidden=false;
  const first=$("modalBody").querySelector("input,textarea");
  if(first)first.focus();
}
function closeModal(){$("modal").hidden=true;}
/* ---------------- refresh & polling ---------------- */
async function refresh(manual){
  try{
    const xs=await api("/api/novels");
    const prev=state.novel?state.novel.id:"";
    state.novels=xs;
    const pick=xs.find(function(x){return x.id===prev;})||(xs[0]||null);
    $("novels").innerHTML=xs.length
      ? xs.map(function(n){return '<option value="'+esc(n.id)+'">'+esc(n.title)+'（已到第 '+esc(n.current_chapter)+' 章）</option>';}).join("")
      : '<option value="">（没有小说，请先新建）</option>';
    if(pick){
      state.novel=pick;$("novels").value=pick.id;
      const res=await Promise.all([
        api("/api/novels/"+pick.id),
        api("/api/novels/"+pick.id+"/chapters"),
        api("/api/novels/"+pick.id+"/jobs"),
        api("/api/novels/"+pick.id+"/usage"),
        api("/api/novels/"+pick.id+"/runs/latest")
      ]);
      state.novel=res[0];state.chapters=res[1]||[];state.jobs=res[2]||[];state.usage=res[3]||[];
      state.run=(res[4]&&res[4].run)||null;
    }else{
      state.novel=null;state.chapters=[];state.jobs=[];state.usage=[];state.run=null;
      if(state.selected)state.selected=null;
    }
    setConn(true);
    ensureEventStream();
    const sig=signature();
    if(sig!==state.sig||manual){state.sig=sig;renderAll();}
    updateBusyUI();
  }catch(e){
    setConn(false);
    if(e.status===401)setBanner({kind:"config",title:"服务端要求访问凭证（401）",message:"请点击右上角 🔑 输入服务端配置的 NOVEL_AUTH_TOKEN 并保存。",fix:"密钥由部署方提供；保存后凭证会自动附加到每个请求。"});
    else if(e.network||e.status===undefined)
      setBanner({kind:"offline",title:"无法连接本地服务端",message:"请确认服务端进程已在运行。",fix:"在项目根目录运行：python3 -m novel_agent.server\n（另开一个终端运行：python3 -m novel_agent.worker）"});
    else toast("刷新失败："+e.message,"err");
  }
}
function updateBusyUI(){
  const active=hasActive();
  state.active=active;
  $("progress").hidden=!active;
  $("busyTag").hidden=!active;
  $("generate").disabled=!state.novel||!!state.novel.paused||active;
  $("pause").disabled=!state.novel;
}
let pollTimer=null;
function schedule(ms){
  if(pollTimer)clearTimeout(pollTimer);
  pollTimer=setTimeout(pollTick,ms||3000);
}
async function pollTick(){
  if(!document.hidden)await refresh(false);
  schedule(state.active?1500:4000);
}

/* ---------------- theme ---------------- */
function applyTheme(t){
  document.documentElement.dataset.theme=t;
  localStorage.setItem("theme",t);
  $("themeBtn").textContent=t==="dark"?"☀️":"🌙";
}
$("themeBtn").addEventListener("click",function(){
  const cur=document.documentElement.dataset.theme==="dark"?"dark":"light";
  applyTheme(cur==="dark"?"light":"dark");
});
(function initTheme(){
  const saved=localStorage.getItem("theme");
  if(saved)return applyTheme(saved);
  applyTheme(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");
})();
/* ---------------- auth ---------------- */
$("authToken").value=token();
$("saveToken").addEventListener("click",function(){
  localStorage.setItem("novelAuthToken",$("authToken").value.trim());
  $("connDetails").open=false;
  toast("访问凭证已保存","ok");
  resetBanner();stopEventStream();refresh(true);
});
$("clearToken").addEventListener("click",function(){
  localStorage.removeItem("novelAuthToken");$("authToken").value="";
  toast("已清除访问凭证","info");resetBanner();stopEventStream();refresh(true);
});
$("authToken").addEventListener("click",function(e){e.stopPropagation();});
$("connDetails").addEventListener("click",function(e){e.stopPropagation();});

/* ---------------- bible ---------------- */
$("bible").addEventListener("input",function(){state.bibleDirty=true;$("dirtyTag").classList.add("on");});
$("fmtBible").addEventListener("click",function(){
  try{$("bible").value=JSON.stringify(JSON.parse($("bible").value),null,2);state.bibleDirty=true;$("dirtyTag").classList.add("on");toast("已格式化","ok");}
  catch(e){toast("JSON 无法格式化："+e.message,"err");}
});
$("saveBible").addEventListener("click",async function(){
  const n=state.novel;if(!n)return toast("请先选择小说","warn");
  let obj;try{obj=JSON.parse($("bible").value);}catch(e){return toast("JSON 语法错误："+e.message,"err");}
  if(typeof obj!=="object"||Array.isArray(obj))return toast("StoryBible 必须是 JSON 对象","warn");
  try{
    const r=await api("/api/novels/"+n.id+"/story-bible",{method:"PATCH",body:JSON.stringify({storyBible:obj})});
    state.bibleDirty=false;
    toast("已保存 StoryBible 新版本 v"+(r&&r.version!==undefined?r.version:(n.story_bible_version+1)),"ok");
    await refresh(true);
  }catch(e){errToast(e,"保存失败：");}
});

/* ---------------- novel bar ---------------- */
$("novels").addEventListener("change",function(){refresh(true);});
$("generate").addEventListener("click",generateNext);
$("resume").addEventListener("click",async function(){
  const n=state.novel;if(!n)return;
  const cfg=flowConfig();
  const body=cfg?JSON.stringify({config:cfg}):"{}";
  try{
    await api("/api/novels/"+n.id+"/continue",{method:"POST",body:body});
    toast("已恢复写作并生成下一章","ok");
    await refresh(true);
    const rc=state.chapters.find(function(x){return x.number===(n.current_chapter||0)+1;});
    if(rc){state.selected=rc.id;renderChapterList();renderReader();}
  }catch(e){
    if(e.status===409&&/confirm_previous_chapter_first/.test(e.data.message||"")){
      const num=(e.data.details&&e.data.details.chapterNumber)||(n.current_chapter||0)+1;
      toast("已恢复写作，但第 "+num+" 章尚未完成「批准 → 导出 → 🚀已人工发布」确认。","warn");
    }else errToast(e);
  }
});
$("pause").addEventListener("click",async function(){
  const n=state.novel;if(!n)return;
  try{await api("/api/novels/"+n.id+"/pause",{method:"POST",body:"{}"});toast("已暂停写作","ok");await refresh(true);}
  catch(e){errToast(e);}
});
$("toggleNew").addEventListener("click",toggleNew);
$("newClose").addEventListener("click",closeNew);
$("createNovel").addEventListener("click",async function(){
  const title=$("newTitle").value.trim();
  if(!title)return toast("请填写书名","warn");
  try{
    let sb={};const raw=$("newBible").value.trim();
    if(raw){sb=JSON.parse(raw);if(typeof sb!=="object"||Array.isArray(sb))throw Error("世界观必须是 JSON 对象");}
    await api("/api/novels",{method:"POST",body:JSON.stringify({title:title,genre:$("newGenre").value.trim(),volume:$("newVolume").value.trim(),storyBible:sb})});
    $("newTitle").value="";$("newGenre").value="";$("newVolume").value="";$("newBible").value="";
    closeNew();toast("小说《"+title+"》已创建","ok");
    await refresh(true);
  }catch(e){toast("创建失败："+e.message,"err");}
});
$("modal").addEventListener("mousedown",function(e){if(e.target.id==="modal")closeModal();});

/* ---------------- boot ---------------- */
refresh(true);
schedule(3000);
