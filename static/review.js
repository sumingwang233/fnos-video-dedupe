// static/review.js —— 审阅页草稿串行保存（Review 26.10.12 §3：草稿保存竞态）。
// 页面只注入 JSON 配置 <script type=application/json id=vd-review-cfg>
// （{"scan","rev","csrf","px","confirmed"}），保存逻辑全在本文件（X1 先例同 app.js）。
// 状态机（§3）：editSeq/savedSeq 双编辑序号——任何选择变化 editSeq++；
// saveLatest() 串行循环在 savedSeq<editSeq 时提交当次快照，成功后只推进
// savedSeq=submittedSeq 并继续；dirty 恒等于 savedSeq!==editSeq，飞行中旧请求的
// 成功回调绝不直接把状态置「干净」，飞行中的新编辑保持未保存、由循环补提交。
// 409（他人已保存更新）：停止一切自动保存并提示刷新——绝不自动拿新修订重试覆盖。
var VD_REVIEW_CFG=null;
try{VD_REVIEW_CFG=JSON.parse(document.getElementById('vd-review-cfg').textContent);}catch(e){}
var VD_SCAN=(VD_REVIEW_CFG&&VD_REVIEW_CFG.scan)||'',
    VD_CSRF=(VD_REVIEW_CFG&&VD_REVIEW_CFG.csrf)||'',
    VD_PX=(VD_REVIEW_CFG&&VD_REVIEW_CFG.px)||'';
var vdRev=(VD_REVIEW_CFG&&VD_REVIEW_CFG.rev)||0;
// §2（Review 26.10.12）：确认快照随草稿恢复（app.js 声明全局，此处赋值）
confirmedByGroup=(VD_REVIEW_CFG&&VD_REVIEW_CFG.confirmed)||{};
var editSeq=0,savedSeq=0,vdLoop=null,vdErr=null,vdTimer=null,vdFrozen=false;
function vdDirty(){return savedSeq!==editSeq;}
function vdPaint(msg){
  var b=document.getElementById('draft-st'),btn=document.getElementById('plan-btn');
  if(!b)return;
  if(vdErr==='conflict'){b.className='badge b-err';b.textContent='草稿冲突：另一标签页已保存更新——请刷新页面重新加载；本页选择未提交';}
  else if(vdErr){b.className='badge b-err';b.textContent='草稿保存失败：'+(msg||'网络错误')+'；本页选择已保留，恢复保存前无法提交';}
  else if(vdDirty()){b.className='badge b-warn';b.textContent='正在保存草稿…';}
  else{b.className='badge b-ok';b.textContent='草稿已保存（修订 '+vdRev+'）';}
  if(btn)btn.disabled=vdFrozen||vdDirty()||!!vdErr;
}
function vdCollect(){
  var out={};
  document.querySelectorAll('.card[data-gid]').forEach(function(card){
    var clean=[];
    card.querySelectorAll('.tile input[type=checkbox]').forEach(function(cb){
      if(cb.checked){clean.push(cb.value);}
    });
    // §2（Review 26.10.12）：confirmed 只读确认时刻的快照（页面载入从草稿恢复、
    // confirmDiff 写入、setTile 在选择实际变化时清空）——绝不用当前 clean 现生成，
    // 否则选择一变确认仍随勾选自动跟进而形同虚设。
    out[card.dataset.gid]={clean:clean,confirmed:confirmedByGroup[card.dataset.gid]||[],skip:card.dataset.skip==='1',
      skip_reason:card.dataset.skipReason||''};
  });
  return out;
}
// §3 请求包装：非 2xx、409、响应非 JSON 或缺 rev 一律 reject——绝不伪成功。
// 409 以 err.conflict=true 标注，调用方据此停止自动覆盖并提示刷新。
function postDraft(rev,entries){
  var body=new URLSearchParams({csrf:VD_CSRF,scan:VD_SCAN,rev:String(rev),draft:JSON.stringify(entries)});
  return fetch(VD_PX+'/draft',{method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'},body:body.toString()})
    .then(function(r){return r.text().then(function(txt){
      var j=null;
      try{j=JSON.parse(txt);}catch(e){}
      if(r.ok&&j&&typeof j.rev==='number')return j;
      var err=new Error((j&&j.error)||('HTTP '+r.status));
      if(r.status===409)err.conflict=true;
      throw err;
    });},function(){throw new Error('网络错误');});
}
// §3 串行循环全局唯一：自动保存、翻页/筛选前保存、生成计划前保存共用同一个
// 循环（后来者等在途循环），绝不并行第二个保存请求。
function saveLatest(){
  if(vdLoop)return vdLoop;
  vdLoop=(async function(){
    while(vdDirty()){
      if(vdErr==='conflict'){var c=new Error('草稿冲突：请刷新页面重新加载');c.conflict=true;throw c;}
      var submitted=editSeq,entries=vdCollect(),rev=vdRev;  // 当次快照与提交序号
      try{
        var res=await postDraft(rev,entries);
        vdRev=res.rev;
        savedSeq=submitted;  // 只推进到本次提交的序号；飞行中的新编辑仍由循环补存
        vdErr=null;
        vdPaint();
      }catch(err){  // 循环停止：编辑与错误提示保留、提交禁用，下次编辑/提交再重试
        vdErr=err&&err.conflict?'conflict':((err&&err.message)||'网络错误');
        vdPaint(vdErr);
        throw err;
      }
    }
  })();
  var settle=function(){vdLoop=null;};
  vdLoop.then(settle,settle);
  return vdLoop;
}
function vdMarkDirty(){
  if(!VD_SCAN||vdErr==='conflict')return;  // §3 冲突后必须刷新重载：不再自动保存覆盖别人
  editSeq++;
  vdErr=null;  // 新编辑清除普通失败态，随本次防抖重试
  vdPaint();
  if(vdTimer)clearTimeout(vdTimer);
  vdTimer=setTimeout(function(){vdTimer=null;saveLatest().catch(function(){});},800);
}
function toggleSkip(btn){
  var card=btn.closest('.card');
  var on=card.dataset.skip!=='1';
  if(on){
    // §三.3：不支持判断的组允许跳过，但提交清理清单前必须说明原因——跳过时
    // 就地收集（可留空，留空的组在提交时被服务端拒绝并点名）。
    var reason=prompt('说明跳过本组的原因（提交清理清单前需要说明，例如：无法判断是否同一内容）：',
                      card.dataset.skipReason||'');
    if(reason===null)return;          // 取消 = 不跳过
    card.dataset.skipReason=reason.trim();
  }
  card.dataset.skip=on?'1':'0';
  if(on)clearPicks(card);  // 跳过 = 整组不清理，勾选清空
  btn.textContent=on?'取消跳过':'跳过本组';
  var b=card.querySelector('.skip-badge');
  if(b)b.style.display=on?'':'none';
  vdMarkDirty();
}
// §3 生成计划前冻结/解冻选择控件：等待串行保存完成期间不允许改动选择
function vdFreeze(on){
  vdFrozen=on;
  document.querySelectorAll('button,.tile input[type=checkbox]').forEach(function(el){
    if(on){if(!el.disabled){el.dataset.vdEnable='1';el.disabled=true;}}
    else if(el.dataset.vdEnable){el.disabled=false;delete el.dataset.vdEnable;}
  });
  vdPaint();
}
if(VD_SCAN){
  (function(){
    var f=document.getElementById('plan-form');
    if(f)f.addEventListener('submit',function(e){
      // §3：冻结选择等 saveLatest() 串行保存成功，再以最新修订号提交
      // （draft_rev 与服务端草稿修订不符会 409）；失败解冻留在本页。
      e.preventDefault();
      if(vdErr==='conflict'){vdPaint();return;}  // 冲突必须刷新，绝不代为提交
      vdFreeze(true);
      saveLatest().then(function(){
        f.querySelector('input[name=draft_rev]').value=String(vdRev);
        f.submit();  // 脚本提交不再派发 submit 事件——不会递归拦截
      },function(){
        vdFreeze(false);  // 保存失败：解冻；错误由状态条呈现、提交按钮保持禁用
      });
    });
    document.addEventListener('click',function(e){  // 翻页/筛选前保存：与自动保存共用循环
      var a=e.target&&e.target.closest?e.target.closest('a[href]'):null;
      if(!a||!vdDirty()||vdErr==='conflict'||vdFrozen)return;
      e.preventDefault();
      saveLatest().then(function(){location.href=a.href;},function(){});  // 失败留在本页重试
    });
    window.addEventListener('beforeunload',function(e){
      // 未保存即离开的警告；不在此发起保存——不承诺异步请求必达（§3）
      if(vdDirty()&&vdErr!=='conflict'){e.preventDefault();e.returnValue='';}
    });
  })();
  vdPaint();
}
