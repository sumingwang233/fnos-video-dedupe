// static/app.js —— 自 webui.py 页面字符串抽出的全站浏览器脚本（Review 26.10.12 §四E）。
// VD_PX 由页面内联定义（统一网关前缀），接口 URL 一律经它拼接。
// 统一轮询助手：正常 1.5s、失败 3s 重试；done(d) 返回 true 即停止（收尾跳转/刷新自便）。
// 0.2.17/0.2.21 曾两次因重构页面丢掉轮询脚本导致状态页静止，收口到这一处。
function vdPoll(url,tick,done){
  function step(){
    fetch(url,{headers:{'Accept':'application/json'}}).then(r=>r.json()).then(d=>{
      if(done&&done(d))return;
      if(tick)tick(d);
      setTimeout(step,1500);
    }).catch(()=>setTimeout(step,3000));
  }
  setTimeout(step,1500);
}
// 0.2.27 起默认浅色：显式存 'dark' 才深色（旧 'light' 存量自然落浅色）。
function toggleTheme(){
  const el=document.documentElement;
  const dark=el.classList.toggle('dark');
  try{localStorage.vdTheme=dark?'dark':'light'}catch(e){}
}
// 表单确认守卫（Review 26.10.12 P1）：data-confirm 取消即 preventDefault，绝不提交。
// 取代内联 onsubmit='return confirm(…)'——返回值布尔仅同步处理器有效，统一走显式监听。
document.addEventListener('submit',e=>{
  const msg=e.target.getAttribute('data-confirm');
  if(msg!==null&&!window.confirm(msg))e.preventDefault();
});
// 顶栏全局状态胶囊：全站可见任务态（空闲/扫描/计划/删除/出错），点击回概览。
// 前台：运行态 2.5s、空闲 10s；后台隐藏 15s 慢轮询；回到前台立即刷新。
(function(){
  const pill=document.getElementById('st-pill'),txt=document.getElementById('st-txt');
  if(!pill||!txt)return;
  let delay=10000,timer=null;
  function schedule(ms){clearTimeout(timer);timer=setTimeout(step,ms);}
  function draw(p){
    const ph=p.phase||'idle';
    const t=p.total||0,d=p.done||0;
    const pct=t?Math.min(99,Math.round(d*100/t)):null;
    let s='空闲',c='';
    // 扫描三阶段（§三.2）：discover 总数未知不显示百分比；fingerprint/grouping
    // 的 done/total 是真实进度，百分比非伪造。已停止是独立终态，不再混称取消。
    if(ph==='discover'){s='查找视频';c='st-run';}
    else if(ph==='fingerprint'){s='识别内容'+(pct!==null?' '+pct+'%':'');c='st-run';}
    else if(ph==='grouping'){s='整理结果'+(pct!==null?' '+pct+'%':'');c='st-run';}
    else if(ph==='planning'){s='生成计划'+(pct!==null?' '+pct+'%':'');c='st-run';}
    else if(ph==='applying'){s='删除中'+(pct!==null?' '+pct+'%':'');c='st-run';}
    else if(ph==='error'){s='出错';c='st-err';}
    else if(ph==='cancelled'){s='扫描已停止';}
    else if(ph==='apply_done'){s='清理完成';c='st-ok';}
    else if(ph==='plan_ready'){s='待确认删除';c='st-ok';}
    else if(ph==='done'){s='扫描完成';c='st-ok';}
    txt.textContent=s;
    pill.className='pill '+c;
    delay=(c==='st-run')?2500:10000;
  }
  function step(){
    fetch(VD_PX+'/progress',{headers:{'Accept':'application/json'}}).then(r=>r.json())
      .then(d=>{draw(d);schedule(document.hidden?15000:delay);})
      .catch(()=>schedule(document.hidden?30000:5000));
  }
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)schedule(0);});
  schedule(400);
})();
function setTile(tile,on){
  if(tile.classList.contains('prot'))return;
  const cb=tile.querySelector('input');
  cb.checked=on;
  tile.classList.toggle('sel',on);
  const st=tile.querySelector('[data-st]');
  if(st){st.textContent=on?'待清理':'保留';st.className='badge corner '+(on?'b-err':'b-ok');}
  const btn=tile.querySelector('.pickbtn');
  if(btn){btn.textContent=on?'改为保留':'标记待清理';btn.setAttribute('aria-pressed',on?'true':'false');}
  if(typeof vdMarkDirty==='function')vdMarkDirty();  // 审阅页：选择变化进草稿（§四D）；其他页面无此函数
}
// §三.3「点击画面进入比较，不兼任删除勾选」：勾选只由 tile 内显式按钮承担
// （键盘可达性走原生 button）；画面/名称是进入比较页的链接（thumblnk）。
function togglePick(btn){
  const tile=btn.closest('.tile');
  const card=tile.closest('.card');
  if(card&&card.dataset.skip==='1')return;  // 已跳过的组不参与本次清理，不响应勾选
  setTile(tile,!tile.querySelector('input').checked);
}
// §三.3「存在差异时在该组内明确确认」：确认状态存草稿 ent.confirmed（=确认
// 时刻的待清理集合，之后改动选择需再次确认；提交校验 clean ⊆ confirmed）。
function confirmDiff(btn){
  const card=btn.closest('[data-gid]');
  const clean=[...card.querySelectorAll('.tile input[type=checkbox]:checked')].map(cb=>cb.value);
  if(!clean.length)return;
  card.dataset.confirmed='1';
  btn.textContent='已确认差异，可提交';
  if(typeof vdMarkDirty==='function')vdMarkDirty();
}
// 完全相同文件的批量建议（Review 26.10.12 §三.3/P0）：初次进入不选中任何
// 清理项；用户点「应用建议」才按服务端建议批量勾选。clean=标记待清理，
// reset=建议保留（含受保护副本）。被跳过（字幕差异/读取异常）的组与画面
// 相似成员不在表内——应用建议不会改动它们的人工判断。
var VD_SUGGEST={};
function applySuggest(gid){
  const map={};
  document.querySelectorAll('.tile input[type=checkbox]').forEach(cb=>map[cb.value]=cb.closest('.tile'));
  for(const g in VD_SUGGEST){
    if(gid&&g!==gid)continue;
    const s=VD_SUGGEST[g];
    s.clean.forEach(k=>{if(map[k])setTile(map[k],true);});
    s.reset.forEach(k=>{if(map[k])setTile(map[k],false);});
  }
}
function clearPicks(scope){
  (scope||document).querySelectorAll('.tile').forEach(t=>setTile(t,false));
}