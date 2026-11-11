'use strict';
const bjtTime = value => new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date(value))+' (北京时间)';
const $ = id=>document.getElementById(id);
let csrf='', current=null, pending=false, appSignature='', activeApp='', selfActionSignature='', selfHistorySignature='';
let selfActionCatalog=[], displayedAnnouncement=null;
function show(msg,isError=false){const n=$('notice');n.textContent=msg;n.className='notice show'+(isError?' error':'');}
async function req(path,method='GET',body){const headers={};if(method!=='GET')headers['X-Client-CSRF']=csrf;if(body)headers['Content-Type']='application/json';
 const r=await fetch('/client/api/'+path,{method,credentials:'same-origin',headers,cache:'no-store',body:body?JSON.stringify(body):undefined});
 let p;try{p=await r.json();}catch{p={};}if(!r.ok)throw Error(p.detail||('HTTP '+r.status));return p;}

 function showCopyableSecret(label,value,reportedAt){
   const overlay=document.createElement('div');overlay.className='vmo-secret-overlay';
   overlay.setAttribute('role','dialog');overlay.setAttribute('aria-modal','true');
   const card=document.createElement('div');card.className='vmo-secret-card';
   const heading=document.createElement('h3');heading.textContent=label+' · 授权信息';
   const hint=document.createElement('p');hint.textContent='此处保存的是最后一次实际上报的访问码。软件重启后若换码，旧码可能失效。';
   const reported=document.createElement('p');reported.textContent='访问码上报时间：'+(reportedAt?bjtTime(reportedAt):'历史记录（时间未知）');
   const field=document.createElement('input');field.type='text';field.readOnly=true;field.value=String(value||'');field.autocomplete='off';field.spellcheck=false;field.setAttribute('aria-label','可复制的应用访问码');
   const buttons=document.createElement('div');buttons.className='vmo-secret-actions';
   const copy=document.createElement('button');copy.type='button';copy.textContent='复制授权码';
   const done=document.createElement('button');done.type='button';done.textContent='关闭';
   const feedback=document.createElement('small');feedback.setAttribute('role','status');
   const close=()=>{field.value='';overlay.remove();document.removeEventListener('keydown',esc);};
   const esc=e=>{if(e.key==='Escape')close();};
   copy.onclick=async()=>{
     try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(field.value);feedback.textContent='已复制';}
       else {field.focus();field.select();const ok=document.execCommand('copy');feedback.textContent=ok?'已复制':'已选中，请按 Ctrl+C 复制';}}
     catch(e){field.focus();field.select();feedback.textContent='已选中，请按 Ctrl+C 复制';}
   };
   done.onclick=close;overlay.onclick=e=>{if(e.target===overlay)close();};
   buttons.append(copy,done);card.append(heading,hint,reported,field,buttons,feedback);overlay.append(card);
   document.body.append(overlay);document.addEventListener('keydown',esc);field.focus();field.select();
 }

function element(tag,text,cls=''){const n=document.createElement(tag);n.textContent=text||'';if(cls)n.className=cls;return n;}
function confirmName(message){const entered=prompt(message+'\n\n请输入本机名称 '+current.name+' 确认：');return entered===current.name;}
async function action(op,extra={}){if(pending)return;pending=true;
 try{const res=await req('action','POST',{action:op,...extra});show('已提交后台任务：'+(res.job_id||res.task_id||'正在处理'));await refresh();}
 catch(e){show('操作失败：'+e.message,true);}finally{pending=false;}}
async function refresh(){
 let m;
 try {m=await req('me');}
 catch(e){
   const expired=/session|401|expired|reopen/i.test(e.message);
   show((expired?'登录会话失效，请从思博业务系统重新打开。':'虚拟机基础信息获取失败：'+e.message+'；请管理员检查 8765 服务日志。'),true);
   if(expired)$('main').hidden=true;
   return;
 }
 current=m;csrf=m.csrf;$('main').hidden=false;
 $('vmName').textContent=m.name;$('serviceName').textContent='业务实例：'+m.service_ref;
 $('serviceExpiry').textContent='到期时间：'+(m.expires_at?bjtTime(m.expires_at):'旧业务未设置期限');
 $('vmIp').textContent='客户机上报 IPv4：'+((m.guest_ipv4_addresses||[]).join('、')||'未上报');
 $('power').textContent=({POWERED_ON:'运行中',POWERED_OFF:'已关机',UNKNOWN:'未知',SUSPENDED:'挂起'})[m.power_state]||m.power_state;
 $('vmstate').textContent=({READY:'已就绪',CREATING:'克隆中',ERROR:'异常',REINSTALLING:'重装中'})[m.vm_state]||m.vm_state;
 $('guest').textContent=m.guest_registered?'已注册':'未注册';
 const off=m.power_state==='POWERED_OFF'&&m.vm_state==='READY',on=m.power_state==='POWERED_ON'&&m.vm_state==='READY';
 $('start').disabled=!off;$('stop').disabled=!on;$('hard').disabled=!on;$('reinstall').disabled=!off || (m.usage&&m.limits&&m.usage.reinstall_used>=m.limits.monthly_reinstall_limit);
 $('snapCreate').disabled=!off;
 // Optional panels fail independently. Do not destroy the app iframe or other controls.
 const panels=await Promise.allSettled([req('snapshots'),req('self-actions'),req('apps'),req('jobs'),req('self-actions/history')]);
 const failures=[];
 if(panels[0].status==='fulfilled'){
   const snaps=panels[0].value;
   $('snapCount').textContent=snaps.count+' / '+snaps.limit;$('snapCreate').disabled=!snaps.can_create;
   $('snapList').replaceChildren();for(const s of snaps.snapshots){const row=element('div','','item');row.append(element('span',s));
   const actions=element('div','','actions');for(const [txt,op] of [['恢复','snapshot_restore'],['删除','snapshot_delete']]){const b=element('button',txt);b.disabled=!off || (op==='snapshot_restore'&&current?.usage?.restore_used>=current?.limits?.monthly_restore_limit);
   b.onclick=()=>{if(!confirmName((op==='snapshot_restore'?'恢复快照会丢失之后的数据。':'删除快照无法撤销。')+' 快照：'+s))return;action(op,{snapshot:s,confirm:true,confirm_name:m.name});};actions.append(b);}row.append(actions);$('snapList').append(row);}
   if(!snaps.snapshots.length)$('snapList').append(element('p','还没有快照','muted'));
 }else failures.push('快照：'+panels[0].reason.message);
 if(panels[1].status==='fulfilled')renderSelfActions(panels[1].value);
 else failures.push('自助操作：'+panels[1].reason.message);
 if(panels[2].status==='fulfilled')renderApps(panels[2].value);
 else failures.push('应用：'+panels[2].reason.message);
 if(panels[3].status==='fulfilled'){
   const jobs=panels[3].value;$('jobs').replaceChildren();for(const j of jobs){const row=element('div','','item');row.append(element('span',j.action+' · '+j.status+' · '+(j.completed_at?'完成：':'提交：')+bjtTime(j.completed_at||j.created_at)));$('jobs').append(row);}if(!jobs.length)$('jobs').append(element('p','暂无任务记录','muted'));
 }else failures.push('任务：'+panels[3].reason.message);
 if(panels[4].status==='fulfilled')renderSelfHistory(panels[4].value);
 else failures.push('自助记录：'+panels[4].reason.message);
 if(failures.length)show('部分模块暂不可用（不影响其他模块）：'+failures.join('；'),true);
 else {$('notice').textContent='';$('notice').className='notice';}
}
async function executeSelfAction(a){
 if(pending)return;
 if(!confirm('确定执行「'+a.label+'」？\n'+(a.description||'请确认你了解此操作的影响。')+'\n\n提交后无法撤销，执行过程中请不要重复点击。'))return;
 pending=true;
 try{
  const id=crypto.randomUUID();
  const result=await req('self-actions/'+encodeURIComponent(a.id)+'/execute','POST',{request_id:id,confirm:true});
  show('自助操作已提交：'+a.label+'（'+result.status+'）。请查看下方任务记录。');
  await refreshSelfHistory();
 }catch(e){show('自助操作失败：'+e.message,true);}
 finally{pending=false;}
}
function renderSelfActions(items){
 const sig=JSON.stringify(items.map(a=>[a.id,a.label,a.description,a.kind,a.sort_order,a.updated_at]));
 if(selfActionSignature===sig)return;
 selfActionSignature=sig;selfActionCatalog=items;
 const select=$('selfActionSelect'),before=select.value;
 select.replaceChildren(new Option('选择自助操作',''));
 for(const a of items)select.add(new Option(a.label+(a.kind==='SOFTWARE_UPDATE'?' · 软件更新':''),a.id));
 if(items.some(a=>a.id===before))select.value=before;
 const changed=()=>{const a=selfActionCatalog.find(a=>a.id===select.value);$('selfActionDescription').textContent=a?(a.description||'管理员预设的快捷操作'):'管理员尚未选择操作';$('selfActionExecute').disabled=!a;};
 select.onchange=changed;changed();
}
function renderSelfHistory(items){
 const sig=JSON.stringify(items.map(a=>[a.request_id,a.status,a.updated_at,a.outcome,a.failure_reason]));
 if(selfHistorySignature===sig)return;
 selfHistorySignature=sig;const target=$('selfHistory');target.replaceChildren();
 const labels={RESERVING:'正在下发',QUEUED:'已排队',CLAIMED:'已接收',RUNNING:'执行中',SUCCEEDED:'成功',FAILED:'失败',TIMED_OUT:'结果未知',UNKNOWN:'结果未知',VERIFY_REQUIRED:'已执行，待核验'};
 for(const a of items.slice(0,5)){const row=element('div','','item');
   const label=a.status==='SUCCEEDED'&&a.outcome==='START_ACCEPTED'?'已启动':(labels[a.status]||a.status);
   const suffix=a.failure_reason?' · '+a.failure_reason:'';
   row.append(element('span',label+' · '+bjtTime(a.updated_at||a.created_at)+suffix));
   target.append(row);
 }
 if(!items.length)target.append(element('p','暂无自助操作记录。','muted'));
}
async function checkCustomerAnnouncement(){
 if(document.hidden)return;
 try{const data=await req('announcement');const a=data.announcement;
   if(!a||displayedAnnouncement===a.id)return;
   displayedAnnouncement=a.id;$('customerAnnouncementText').textContent=a.body;
   $('customerAnnouncementModal').hidden=false;
 }catch(e){console.warn('公告检查失败',e.message);}
}
async function acknowledgeCustomerAnnouncement(){
 if(!displayedAnnouncement)return;
 try{await req('announcement/'+encodeURIComponent(displayedAnnouncement)+'/ack','POST',{});
   $('customerAnnouncementModal').hidden=true;
 }catch(e){show('公告确认失败：'+e.message,true);}
}
async function refreshSelfHistory(){try{renderSelfHistory(await req('self-actions/history'));}catch(e){show('自助任务状态同步失败：'+e.message,true);}}
function launchForm(gateway,ticket,target){
 const f=document.createElement('form');f.method='POST';f.action=gateway;f.target=target;
 f.style.display='none';const field=document.createElement('input');field.name='ticket';field.value=ticket;
 f.append(field);document.body.append(f);f.submit();setTimeout(()=>f.remove(),2000);
}
async function openApp(appId,newTab=false){
 // Popup must be created synchronously from a real click, before any await.
 const tab=newTab?window.open('about:blank','_blank'):null;
 if(newTab&&!tab)return show('浏览器阻止了新窗口，请允许弹窗或使用页面内打开',true);
 try{
   const r=await req('apps/'+encodeURIComponent(appId)+'/launch','POST');
   if(newTab){const label='vmo-app-'+Date.now();tab.name=label;launchForm(r.gateway,r.ticket,label);}
   else{
     activeApp=appId;$('appViewerTitle').textContent='应用网页 · '+(appId==='snowluma'?'SnowLuma':'DSH-fish-soul');
     $('appViewer').hidden=false;$('appFrame').name='vmo-application-frame';launchForm(r.gateway,r.ticket,'vmo-application-frame');
     $('appViewer').scrollIntoView({behavior:'smooth',block:'nearest'});
   }
 }catch(e){if(tab)tab.close();show('打开应用失败：'+e.message,true);}
}
async function refreshApps(){
 const items=await req('apps');
 renderApps(items);
}
async function refreshSelfActions(){renderSelfActions(await req('self-actions'));}
function renderApps(items){
 const signature=JSON.stringify(items.map(a=>[a.app_id,a.auth_state,a.version,a.web_access,a.online,a.last_report,a.secret_reported_at]));
 if(appSignature===signature)return; // Don't destroy existing app controls on periodic sync.
 appSignature=signature;const target=$('myApps');target.replaceChildren();
 for(const a of items){
   const r=element('div','','item');const title=a.app_id==='snowluma'?'SnowLuma':'DSH-fish-soul';
   const authLabel={configured:'已上报访问码（有效性未验证）',unconfigured:'尚未登记访问码',not_reported:'访问码未上报'}[a.auth_state]||a.auth_state;
   r.append(element('span',title+' · 授权：'+authLabel+' · '+(a.version||'版本未上报')+' · '+(a.online?'运行中':'未连接或未检测到运行')));
   r.append(element('small','访问码上报：'+(a.secret_reported_at?bjtTime(a.secret_reported_at):'未上报或历史时间未知'),'muted'));
   const actions=element('div','','actions');
   const open=element('button','在本页打开');open.disabled=a.web_access!=='https_wss_relay';
   open.onclick=()=>openApp(a.app_id,false);actions.append(open);
   const tab=element('button','新标签页打开');tab.disabled=open.disabled;
   tab.onclick=()=>openApp(a.app_id,true);actions.append(tab);
   const code=element('button','查看授权信息');code.onclick=async()=>{
     if(!confirm('授权码属于敏感信息，仅在当前客户页面临时展示。继续？'))return;
     try{const response=await req('apps/'+encodeURIComponent(a.app_id)+'/reveal','POST',{confirm:true});
       showCopyableSecret(title,response.access_code,response.reported_at);
     }catch(e){show('读取授权失败：'+e.message,true);}
   };actions.append(code);r.append(actions);target.append(r);
 }
 if(!items.length)target.append(element('p','暂未检测到应用入口；请检查应用是否运行、Agent 是否已上报','muted'));
}
async function refreshLive(){
 // Never re-render an app iframe, snapshots, or active forms on timer ticks.
 if(document.hidden||pending)return;
 try{const m=await req('me');current=m;csrf=m.csrf;
   $('power').textContent=({POWERED_ON:'运行中',POWERED_OFF:'已关机',UNKNOWN:'未知',SUSPENDED:'挂起'})[m.power_state]||m.power_state;
   $('vmstate').textContent=({READY:'已就绪',CREATING:'克隆中',ERROR:'异常'})[m.vm_state]||m.vm_state;
   $('guest').textContent=m.guest_registered?'已注册':'未注册';
   const off=m.power_state==='POWERED_OFF'&&m.vm_state==='READY',on=m.power_state==='POWERED_ON'&&m.vm_state==='READY';
   $('start').disabled=!off;$('stop').disabled=!on;$('hard').disabled=!on;$('reinstall').disabled=!off || (m.usage&&m.limits&&m.usage.reinstall_used>=m.limits.monthly_reinstall_limit);
   const jobs=await req('jobs');const node=$('jobs');
   const sig=JSON.stringify(jobs.map(j=>[j.id,j.status,j.created_at,j.completed_at]));
   if(node.dataset.signature!==sig){node.dataset.signature=sig;node.replaceChildren();
     for(const j of jobs){const row=element('div','','item');row.append(element('span',j.action+' · '+j.status+' · '+(j.completed_at?'完成：':'提交：')+bjtTime(j.completed_at||j.created_at)));node.append(row);}
     if(!jobs.length)node.append(element('p','暂无任务记录','muted'));
   }
   await refreshSelfHistory();
 }catch(e){show('状态刷新失败：'+e.message,true);}
}
async function screen(){const status=$('screenStatus');status.textContent='正在抓取截图…';try{const res=await fetch('/client/api/screen.png',{credentials:'same-origin',cache:'no-store'});if(!res.ok){let p={};try{p=await res.json();}catch{}throw Error(p.detail||('HTTP '+res.status));}
 const url=URL.createObjectURL(await res.blob());const img=$('screenImage');const old=img.dataset.url;img.src=url;img.hidden=false;img.dataset.url=url;if(old)URL.revokeObjectURL(old);status.textContent='截图时间：'+bjtTime(Date.now());}
 catch(e){status.textContent='截图失败：'+e.message+'（可能尚未启用本机 VNC 截图）';}}
window.addEventListener('DOMContentLoaded',()=>{
 $('refresh').onclick=refresh; $('selfActionExecute').onclick=()=>{const a=selfActionCatalog.find(x=>x.id===$('selfActionSelect').value);if(a)executeSelfAction(a);}; $('customerAnnouncementAck').onclick=acknowledgeCustomerAnnouncement;$('screenRefresh').onclick=screen;
 $('appViewerReload').onclick=()=>{if(activeApp)openApp(activeApp,false);};
 $('appViewerClose').onclick=()=>{$('appViewer').hidden=true;$('appFrame').src='about:blank';activeApp='';};
 $('start').onclick=()=>action('start');$('stop').onclick=()=>{if(confirm('确认正常关闭 Windows？'))action('stop');};
 $('hard').onclick=()=>{if(confirmName('强制关机可能丢失数据'))action('hard_stop',{confirm:true,confirm_name:current.name});};
 $('reinstall').onclick=()=>{if(confirmName('系统重装将更换 Windows 虚拟磁盘，原系统数据可能丢失'))action('reinstall',{confirm:true,confirm_name:current.name});};
 $('snapCreate').onclick=()=>{const name=$('snapName').value.trim();if(!/^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$/.test(name))return show('快照名须是英文字母、数字、下划线或短横线',true);action('snapshot_create',{snapshot:name});};
 $('logout').onclick=async()=>{try{await req('logout','POST');}catch{}window.location.assign('/client');};
 refresh().then(checkCustomerAnnouncement);
 setInterval(refreshLive,6000);
 setInterval(checkCustomerAnnouncement,60000);
 setInterval(()=>{if(!document.hidden&&!pending){
   refreshApps().catch(e=>show('应用状态同步失败：'+e.message,true));
   refreshSelfActions().catch(e=>show('自助操作列表同步失败：'+e.message,true));
 }},20000);
});
