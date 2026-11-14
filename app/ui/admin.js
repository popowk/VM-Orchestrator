/* VM Orchestrator v1.4.57 - admin UI (single served asset) */

/* ===== ui.js ===== */
'use strict';
const VMO_BJT = (value, kind='datetime') => {
 if(!value)return '—';const d=new Date(value);if(Number.isNaN(d.getTime()))return String(value);
 const opts={timeZone:'Asia/Shanghai',hour12:false,hourCycle:'h23',hour:'2-digit',minute:'2-digit',second:'2-digit'};
 if(kind!=='time')Object.assign(opts,{year:'numeric',month:'2-digit',day:'2-digit'});
 return new Intl.DateTimeFormat('zh-CN',opts).format(d)+' (北京时间)';
};
window.VMOTime=VMO_BJT;
let VMOEventSource=null;
function VMOConnectEvents(){
 if(!window.EventSource||VMOEventSource||!state.csrf)return;
 const stream=new EventSource('/api/admin/events');VMOEventSource=stream;
 stream.addEventListener('changed',()=>{if(document.hidden)return;if(state.activePage==='remote'){window.dispatchEvent(new Event('vmo:changed'));}else if(state.activePage==='clients'){window.VMOClients?.refresh();}else refresh();});
}
function VMOCloseEvents(){if(VMOEventSource){VMOEventSource.close();VMOEventSource=null;}}
const $ = id => document.getElementById(id);
const state = { csrf:'', templates:[], vms:[], jobs:[], capabilities:null, viewingVm:null, activePage:'overview', busy:false, watched:new Set(), defaultTemplate:null, guests:[], packages:[], portableProfiles:[], updateTasks:[], reinstalls:[], templateBuilds:[], templateBuildRoot:'', reinstallVm:null, packageSelections:{}, jobsPage:1, updatePage:1, jobsPaged:{items:[],page:1,pages:1,total:0}, updatesPaged:{items:[],page:1,pages:1,total:0} };
const pages = { overview:'管理概览', vms:'虚拟机管理', templates:'黄金模板', updates:'软件更新', jobs:'任务记录', remote:'远程运维', clients:'思博客户服务', system:'系统更新', cleanup:'空间清理' };
const statusName = { READY:'就绪', CREATING:'克隆中', ERROR:'异常', DELETING:'删除中', REINSTALLING:'重装中', REINSTALL_ERROR:'重装异常', DELETE_ERROR:'删除失败', SEALED_ATTESTED:'已封存', CANDIDATE:'待确认', ARCHIVED:'已归档', FILES_PURGING:'磁盘清理异常', FILES_DELETED:'文件已清理（保留历史）', POWERED_ON:'运行中', POWERED_OFF:'已关机', SUSPENDED:'挂起', UNKNOWN:'未知', QUEUED:'排队中', RUNNING:'执行中', SUCCEEDED:'成功', VERIFY_REQUIRED:'已执行，待版本核验', FAILED:'失败' };
function el(tag, cls='', text='') { const v=document.createElement(tag); if(cls)v.className=cls; if(text!==undefined && text!==null)v.textContent=String(text); return v; }
function btn(text, cls, fn, disabled=false) { let e=el('button','btn '+cls,text);e.type='button';e.disabled=disabled;e.addEventListener('click',fn);return e; }
function badge(name) { let cls='';if(['READY','SEALED_ATTESTED','POWERED_ON','SUCCEEDED'].includes(name))cls='ok';else if(['ERROR','FAILED','DELETE_ERROR','FILES_PURGING'].includes(name))cls='bad';else if(['RUNNING','QUEUED','CREATING','DELETING'].includes(name))cls='info';else if(name==='VERIFY_REQUIRED')cls='warn';else if(name==='SUSPENDED')cls='warn';return el('span','pill '+cls,statusName[name]||name||'未知'); }
function toast(msg, error=false) {let v=$('toast');v.textContent=msg;v.className='toast show'+(error?' bad':'');clearTimeout(toast.timer);toast.timer=setTimeout(()=>v.className='toast',6000);}
async function request(path, method='GET', data) {
  const headers={}; if(state.csrf && method!=='GET')headers['X-UI-CSRF']=state.csrf;
  if(data!==undefined)headers['Content-Type']='application/json';
  let res; try {res=await fetch(path,{method,headers,credentials:'same-origin',body:data===undefined?undefined:JSON.stringify(data)});}catch(e){throw Error('无法连接 Host Agent，请检查服务是否运行');}
  let payload;try{payload=await res.json();}catch(e){payload={detail:res.statusText};}
  if(res.status===401){setLogin(true);window.location.replace('/');throw Error('登录会话已失效，请重新登录');}
  if(!res.ok)throw Error(typeof payload.detail==='string'?payload.detail:JSON.stringify(payload.detail||payload));
  return payload;
}
function setLogin(visible){
  // Hide first (before any overlay, redirect or asynchronous session lookup).
  $('adminShell').hidden=visible || !state.csrf;
  $('loginScreen').hidden=!visible;
  if(visible){state.csrf='';VMOCloseEvents();$('loginError').textContent='';}
} 
function navigate(page){
 if(!pages[page])return;
 const previous=state.activePage;
 if(previous==='remote'&&page!=='remote')window.VMORemote?.stopPreview();
 state.activePage=page;
 const url=new URL(window.location.href);
 if(page==='overview')url.searchParams.delete('page');else url.searchParams.set('page',page);
 window.history.replaceState(null,'',url.pathname+url.search+url.hash);
 document.querySelectorAll('.page').forEach(n=>n.classList.toggle('active',n.id==='page-'+page));
 document.querySelectorAll('.nav-item').forEach(n=>n.classList.toggle('active',n.dataset.page===page));
 $('pageTitle').textContent=pages[page];
 if(page==='clients')window.VMOClients?.refresh();
 else if(page==='remote')window.VMORemote?.refresh();
 else if(page==='system'){window.VMOSystemUpdate?.load();window.VMOHostManage?.load();}
 else if(page==='cleanup')window.VMOCleanup?.scan();
 else refresh();
}
async function refresh(){if(state.busy || !state.csrf)return;state.busy=true; try{
  const [tpl,vms,jobs,caps,defaults,guests,packages,profiles,updatesPaged,reinstalls,jobsPaged,buildData] = await Promise.all([
    request('/api/templates'),request('/api/vms'),request('/api/jobs'),request('/api/host/capabilities'),
    request('/api/fleet/default-template'),request('/api/fleet/guests'),request('/api/fleet/packages'),request('/api/fleet/portable/profiles'),
    request(`/api/fleet/updates?paged=true&page=${state.updatePage}&page_size=15`),request('/api/fleet/reinstalls'),
    request(`/api/jobs?paged=true&page=${state.jobsPage}&page_size=20`),request('/api/template-builds')]);
  state.templates=tpl;state.vms=vms;state.jobs=jobs;state.capabilities=caps;
  state.defaultTemplate=defaults.template_id;state.guests=guests;state.packages=packages;state.portableProfiles=profiles;state.updateTasks=updatesPaged.items;state.updatesPaged=updatesPaged;state.updatePage=updatesPaged.page;state.jobsPaged=jobsPaged;state.jobsPage=jobsPaged.page;state.reinstalls=reinstalls;state.templateBuilds=buildData.builds||[];state.templateBuildRoot=buildData.root||'';
  // Only redraw the page currently visible; never replace hidden forms,
  // expanded task details or a customer application viewer on a timer tick.
  if(state.activePage==='overview')renderOverview();
  else if(state.activePage==='templates'){renderTemplates();renderTemplateBuilds();}
  else if(state.activePage==='vms'){renderTemplates();renderVms();}
  else if(state.activePage==='jobs')renderJobs();
  else if(state.activePage==='updates')renderFleet();
  else if(state.activePage==='clients')window.VMOClients?.refresh();
  $('hostDot').className='dot '+(caps.vmrun_responding?'ok':'bad');$('hostLabel').textContent=caps.vmrun_responding?'服务正常':'VMware 工具异常';
  for(const id of Array.from(state.watched)){const j=state.jobs.find(x=>x.id===id);if(j && ['SUCCEEDED','FAILED'].includes(j.status)){state.watched.delete(id);toast(`${j.action}：${statusName[j.status]}${j.detail?' · '+j.detail:''}`,j.status==='FAILED');}}
 }catch(e){$('hostDot').className='dot bad';$('hostLabel').textContent='连接异常';toast(e.message,true);}finally{state.busy=false;}}

function hostBytes(n){const x=Number(n);if(!Number.isFinite(x))return '—';return x>=1073741824?(x/1073741824).toFixed(1)+' GB':x>=1048576?(x/1048576).toFixed(1)+' MB':x>=1024?(x/1024).toFixed(1)+' KB':x+' B';}
async function refreshHostMetrics(){
  const grid=$('hostGauges'),details=$('hostResourceDetails'),stamp=$('hostGaugeTime');if(!grid||!details)return;
  try{
    const m=await request('/api/host/metrics');grid.replaceChildren();details.replaceChildren();
    if(!m.available){grid.append(el('p','muted',m.message||'性能计数器暂不可用'));stamp.textContent='不可用';return;}
    function dial(name,percent,value,info,color){
      const tile=el('div','vmo-gauge-card'),ring=el('div','vmo-gauge-ring'),inner=el('div','vmo-gauge-inner');
      ring.style.setProperty('--gauge-fill',Math.max(0,Math.min(100,Number(percent)||0))+'%');
      ring.style.setProperty('--gauge-color',color);
      inner.append(el('strong','',value),el('small','',name));
      ring.append(inner);tile.append(ring,el('div','vmo-gauge-description',info));grid.append(tile);
    }
    dial('CPU',m.cpu?.percent,(m.cpu?.percent??'—')+'%',(m.cpu?.physical_cores??'—')+' 核 / '+(m.cpu?.logical_cores??'—')+' 线程','#3c70e0');
    dial('内存',m.memory?.percent,(m.memory?.percent??'—')+'%',hostBytes(m.memory?.used)+' / '+hostBytes(m.memory?.total),'#8264d9');
    const disks=Array.isArray(m.disks)?m.disks:[],disk=disks.find(x=>x.available!==false&&Number.isFinite(Number(x.percent)));
    dial('磁盘',disk?.percent,disk?disk.percent+'%':'—',disk?disk.volume+' · 剩余 '+hostBytes(disk.free):'计数器不可用','#d99543');
    const net=el('div','vmo-gauge-card vmo-network-card');
    net.append(el('div','vmo-network-icon','↕'),el('strong','vmo-network-title','网络流量'));
    const ready=m.network?.sample_ready;
    net.append(el('div','vmo-network-value','↓ '+(ready?hostBytes(m.network.download_bps)+'/s':'采样中')),
               el('div','vmo-network-value','↑ '+(ready?hostBytes(m.network.upload_bps)+'/s':'采样中')),
               el('small','muted','宿主机全部网卡合计速率'));
    grid.append(net);
    for(const volume of disks){if(volume.available===false)continue;
      const line=el('div','vmo-host-disk-row');
      line.append(el('span','',volume.label+' · '+volume.volume),el('strong','',hostBytes(volume.used)+' / '+hostBytes(volume.total)));
      details.append(line);
    }
    details.append(el('div','vmo-host-disk-row','宿主机：'+(m.host||'未知')+' · 已运行约 '+Math.floor((m.uptime_seconds||0)/3600)+' 小时'));
    stamp.textContent='采样：'+VMO_BJT(m.sample_time*1000);
  }catch(e){grid.replaceChildren(el('p','muted','读取失败：'+e.message));details.replaceChildren();stamp.textContent='获取失败';}
}

function renderTemplateBuilds(){
  const list=$('templateBuildList'),root=$('templateBuildRoot'),sel=$('templateBuildSource');
  if(!list||!root||!sel)return;
  root.textContent=state.templateBuildRoot;
  const previous=sel.value;sel.replaceChildren(new Option('请选择已封存的黄金模板',''));
  for(const t of state.templates.filter(x=>x.status==='SEALED_ATTESTED'))
    sel.add(new Option(t.name,t.id));
  if([...sel.options].some(x=>x.value===previous))sel.value=previous;
  list.replaceChildren();
  const labels={CREATING:'制作中',READY:'待操作/待封存',
    PUBLISHING:'发布到正式模板库中',PUBLISHED_CANDIDATE:'已发布候选模板',
    FAILED_REVIEW:'失败，保留文件待人工核实',
    INTERRUPTED:'任务中断，保留文件待人工核实'};
  for(const b of state.templateBuilds){
    const row=el('div','template-card'),info=el('div'),actions=el('div');
    info.append(el('strong','',b.name+' · '+(labels[b.status]||b.status)),
       el('small','',b.vmx_path),
       el('small','',b.mode==='iso'?'ISO 全新制作':'从现有黄金模板完整克隆'));
    if(b.error)info.append(el('small','',b.error));
    if(b.status==='READY'){
      actions.append(btn('关机后发布到正式模板候选库','primary small',async()=>{
        if(!confirm('确认模板已完成必要 Windows/OOBE 配置、Sysprep /generalize /shutdown，并且没有运行或挂起，也已断开安装 ISO？\n\n系统会完整克隆到正式模板目录，只生成待人工封存的候选记录，不修改旧模板或任何客户机。'))return;
        try{
          await request('/api/template-builds/'+encodeURIComponent(b.id)+'/publish','POST',{});
          toast('发布候选模板已排队，可在制作列表查看状态');await refresh();
        }catch(e){toast('发布失败：'+e.message,true);}
      }));
    }
    if(b.official_template_id)info.append(el('small','','正式模板 ID：'+
       b.official_template_id+'；请在上方完成人工封存确认'));
    row.append(info,actions);list.append(row);
  }
  if(!list.childElementCount)list.append(el('p','muted','暂无模板制作项目。'));
}

function renderOverview(){
  $('statVm').textContent=state.vms.length;$('statRunning').textContent=state.vms.filter(x=>x.power_state==='POWERED_ON').length;$('statTemplate').textContent=state.templates.filter(x=>x.status==='SEALED_ATTESTED').length;$('statJobs').textContent=state.jobs.filter(x=>['QUEUED','RUNNING'].includes(x.status)).length;
  let caps=$('capabilities');caps.replaceChildren();
  [['vmrun.exe',state.capabilities.vmrun_present],['vmrun 响应',state.capabilities.vmrun_responding],['模板目录',state.capabilities.templates_root_present],['客户 VM 目录',state.capabilities.vms_root_present],['Guest HTTPS 网关已配置',state.capabilities.guest_gateway_configured]].forEach(([name,yes])=>{let row=el('div','kv');row.append(el('span','',name),badge(yes?'SUCCEEDED':'FAILED'));caps.append(row);});
  renderJobTable('overviewJobs',state.jobs.slice(0,5));
   refreshHostMetrics();
}
function renderTemplates(){let wrap=$('templateList');wrap.replaceChildren();let opts=$('cloneTemplate'),old=opts.value;opts.replaceChildren(new Option('请选择已封存模板',''));
 if(!state.templates.length){wrap.append(el('p','empty','还没有注册模板；请点击下方扫描模板目录'));return;}
 state.templates.forEach(t=>{if(t.status==='SEALED_ATTESTED')opts.add(new Option(t.name,t.id));
  let row=el('div','template-card'),info=el('div'),head=el('div');head.append(el('strong','',t.name),el('small','',t.vmx_path),el('small','',`模板 ID：${t.id}`));info.append(head);
  let actions=el('div');actions.append(badge(t.status));if(t.status==='SEALED_ATTESTED')actions.append(badge(t.oobe_bootstrap_version>=2?'Administrator 无人值守 V1.2':t.oobe_bootstrap?'旧版 OOBE（不可全自动）':'普通模板（需手动 OOBE）'));
  if(t.status==='SEALED_ATTESTED'){
    actions.append(btn(t.id===state.defaultTemplate?'★ 默认模板':'设为默认模板','ghost small',async()=>{
      try{await request(`/api/fleet/default-template/${encodeURIComponent(t.id)}`,'POST');toast('默认模板已设置');await refresh();}catch(e){toast(e.message,true);}
    },t.id===state.defaultTemplate));
  }
  if(t.status==='ARCHIVED'){
    actions.append(btn('恢复为候选模板','ghost small',async()=>{
      try{await request(`/api/templates/${encodeURIComponent(t.id)}/restore`,'POST');toast('已恢复成候选模板，需重新确认封存');await refresh();}catch(e){toast(e.message,true);}
    }));
  }
  if(t.status==='CANDIDATE')actions.append(btn('人工封存确认','ghost small',()=>sealTemplate(t)));
  if(t.status!=='ARCHIVED' && t.status!=='FILES_DELETED' && t.status!=='FILES_PURGING')actions.append(btn('移出管理列表','ghost small',()=>archiveTemplate(t)));
  if(t.status==='ARCHIVED')actions.append(btn('永久清理归档磁盘','danger small',()=>removeTemplate(t)));
  if(t.status==='FILES_DELETED')actions.append(el('small','',`文件已清理：${VMO_BJT(t.files_deleted_at)}；可选择丢弃来源记录`));
  if(t.status==='ARCHIVED'||t.status==='FILES_DELETED')actions.append(btn('彻底移除历史登记','danger small',()=>forgetTemplate(t)));
  row.append(info,actions);wrap.append(row);
 });opts.value=Array.from(opts.options).some(x=>x.value===old)?old:'';
}
async function scanTemplates(){
 const box=$('discoveredTemplates');box.replaceChildren(el('p','muted','正在扫描...'));
 try{
  const result=await request('/api/templates/discover');box.replaceChildren();
  if(!result.candidates.length){box.append(el('p','empty',`没有在 ${result.root} 找到 .vmx 文件，请检查启动参数 TemplatesRoot。`));return;}
  for(const item of result.candidates){
   const row=el('div','template-card'),detail=el('div');detail.append(el('strong','',item.suggested_name),el('small','',item.vmx_path));
   let actions=el('div');
   if(item.registered)actions.append(badge(item.status));
   else actions.append(btn('选择并登记','primary small',async()=>{
     const name=prompt('模板显示名称：',item.suggested_name);if(!name)return;
     try{await request('/api/templates','POST',{name:name.trim(),vmx_path:item.vmx_path});toast('模板已登记；注意：仍需真实 Sysprep 和封存确认');await refresh();await scanTemplates();}catch(e){toast(e.message,true);}
   }));
   row.append(detail,actions);box.append(row);
  }
 }catch(e){box.replaceChildren(el('p','empty','扫描失败：'+e.message));}
}
async function forgetTemplate(t){
 const name=prompt(`危险：彻底删除模板「${t.name}」的来源记录、审计事件，并清除已有客户机上的来源关系。\n不会删除已有客户 VM、快照或 Guest Agent。\n如果是根目录模板，磁盘文件必须先人工安全清理。\n输入模板名称确认：`);
 if(name!==t.name)return;
 if(!confirm('最后确认：来源记录永久丢失，不能恢复。该操作不会自动删除 VMX/VMDK。继续？'))return;
 try{const r=await request(`/api/templates/${encodeURIComponent(t.id)}/forget-history`,'POST',{confirm_name:name,confirm_delete_history:true});
   toast(`已移除模板来源记录；保留 ${r.vm_records_preserved} 条客户虚拟机记录`);await refresh();await scanTemplates();}
 catch(e){toast('移除历史失败：'+e.message,true);}
}
async function archiveTemplate(t){
 if(t.id===state.defaultTemplate)return toast('请先切换默认黄金模板',true);
 const name=prompt(`从网页移除模板「${t.name}」？\n文件和已有 VM 都保留。\n输入模板名称确认：`);
 if(name!==t.name)return;
 try{await request(`/api/templates/${encodeURIComponent(t.id)}/archive`,'POST',{confirm_name:name});toast('模板已移出管理列表；磁盘文件仍在');await refresh();}
 catch(e){toast('移除失败：'+e.message,true);}
}
async function removeTemplate(t){
 if(t.id===state.defaultTemplate)return toast('请先切换默认黄金模板',true);
 if(t.status!=='ARCHIVED')return toast('必须先将黄金模板归档',true);
 const name=prompt(`危险：永久清理已归档黄金模板「${t.name}」的独立磁盘目录！\n所有来源记录和历史虚拟机关联将保留。\n根目录或共享目录无法自动清理。\n输入模板名称确认：`);
 if(name!==t.name)return;
 if(!confirm('最终确认：只删除已归档黄金模板的独立 VMX/VMDK/快照目录，保留数据库历史；不可恢复。继续？'))return;
 try{await request(`/api/templates/${encodeURIComponent(t.id)}`,'DELETE',{confirm_name:name,confirm_permanent_delete:true,confirm_keep_history:true});toast('磁盘清理完成，历史关联已保留');await refresh();await scanTemplates();}
 catch(e){toast('永久删除失败：'+e.message,true);}
}
function vmCard(vm){let card=el('div','vm-card'),head=el('div','vm-title'),name=el('strong','',vm.name);head.append(name,badge(vm.power_state==='POWERED_ON'?'POWERED_ON':vm.state==='READY'?vm.power_state:vm.state));card.append(head,el('div','vm-sub',`客户：${vm.customer_ref||'未绑定'}  ·  ${vm.id}`));
 const guest=state.guests.find(x=>x.vm_id===vm.id&&!x.revoked_at);
 const ipv4=guest?.reported?.ipv4_addresses||[];
 if(ipv4.length)card.append(el('div','vm-sub',`客户机自报 IP：${ipv4.join('、')}（不作为授权依据）`));
 let meta=el('div','vm-meta');meta.append(badge(vm.state));if(vm.state==='READY')meta.append(badge(vm.power_state));card.append(meta);
 let actions=el('div','vm-buttons');actions.append(btn('详情 / 快照','ghost',()=>openDetail(vm.id)));
 if(vm.state==='READY'){
  actions.append(btn(vm.power_state==='SUSPENDED'?'恢复运行':'开机','primary',()=>vmAction(vm,'start'),!['POWERED_OFF','SUSPENDED'].includes(vm.power_state)));
  actions.append(btn('软关机','ghost',()=>vmAction(vm,'stop'),vm.power_state!=='POWERED_ON'));
  actions.append(btn('强制关机','danger',()=>hardStop(vm),vm.power_state!=='POWERED_ON'));
  actions.append(btn('状态诊断','ghost',()=>openDiagnostics(vm)));
  actions.append(btn('初始化信息','ghost',()=>openProvision(vm)));
  actions.append(btn('重装系统','warn',()=>openReinstall(vm),vm.power_state!=='POWERED_OFF'||vm.suspend_reinstall_blocked));
  actions.append(btn('软件更新','ghost',()=>{navigate('updates');},false));
 }
 if(vm.state==='ERROR')actions.append(btn('身份修复','warn',()=>repair(vm)));
 if(['READY','ERROR','DELETE_ERROR'].includes(vm.state)) {
  const processing = state.jobs.some(j=>j.vm_id===vm.id && ['QUEUED','RUNNING'].includes(j.status));
  const powerKnownOn = ['POWERED_ON','SUSPENDED'].includes(vm.power_state);
  const del=btn('删除虚拟机','danger',()=>deleteVm(vm),processing || powerKnownOn);
  del.title=processing?'任务执行中不能删除':powerKnownOn?'请先关闭虚拟机':'永久删除该 VM 和目录中的全部磁盘、快照';
  actions.append(del);
 }
 card.append(actions);return card;
}
function renderVms(){let wrap=$('vmList');wrap.replaceChildren();$('vmCount').textContent=`共 ${state.vms.length} 台`;if(!state.vms.length){wrap.append(el('div','empty','暂无虚拟机，先从模板完整克隆一台'));return;}state.vms.forEach(v=>wrap.append(vmCard(v)));}
function renderJobTable(target,list){let wrap=$(target);wrap.replaceChildren();if(!list.length){wrap.append(el('div','empty','还没有任务记录'));return;}
 let table=el('table','table'),thead=el('thead'),tr=el('tr');['时间','操作','虚拟机','状态','结果 / 进度'].forEach(t=>tr.append(el('th','',t)));thead.append(tr);table.append(thead);let tb=el('tbody');
 for(let j of list){let r=el('tr'),vm=state.vms.find(x=>x.id===j.vm_id),date=VMO_BJT(j.created_at);r.append(el('td','',date),el('td','',j.action),el('td','',vm?vm.name:(j.vm_name||(j.vm_id||'').slice(0,8))));
 let td=el('td');td.append(badge(j.status));r.append(td);let last=el('td'),s=el('span','ellipsis',j.detail||'—');s.title=j.detail||'';last.append(s);r.append(last);tb.append(r);}
 table.append(tb);wrap.append(table);
}
function renderPager(target,paging,onPage){
  const container=$(target);if(!container)return;
  container.replaceChildren();if(!paging)return;
  const page=paging.page||1,pages=paging.pages||1,total=paging.total||0;
  const info=el('span','muted',`共 ${total} 条 · 第 ${page} / ${pages} 页`);
  const prev=btn('上一页','ghost small',()=>onPage(page-1),page<=1);
  const next=btn('下一页','ghost small',()=>onPage(page+1),page>=pages);
  container.append(prev,info,next);
}
function renderJobs(){
  renderJobTable('allJobs',state.jobsPaged.items||[]);
  renderPager('jobsPager',state.jobsPaged,page=>{state.jobsPage=page;refresh();});
}
async function submitJob(path,method='POST',data){let ret=await request(path,method,data);if(ret.job_id){state.watched.add(ret.job_id);toast('任务已提交：'+ret.job_id);await refresh();}return ret;}
async function vmAction(vm,what){if(what==='stop' && !confirm(`确认让 ${vm.name} 执行正常软关机？`))return;
 if(what==='start' && vm.power_state==='SUSPENDED' && !confirm(`确定恢复「${vm.name}」挂起前的运行状态？\n不会删除 .vmss 文件或丢弃挂起的内存。`))return;
 try{await submitJob(`/api/vms/${encodeURIComponent(vm.id)}/${what}`,'POST',what==='stop'?{mode:'soft'}:undefined);}catch(e){toast(e.message,true);}}
async function hardStop(vm){
  if(vm.power_state!=='POWERED_ON')return;
  const confirmation=prompt(`警告：强制关机会像拔电源一样中断 Windows，可能导致数据丢失或系统损坏。\n\n输入虚拟机名称 ${vm.name} 确认强制关机：`);
  if(confirmation!==vm.name){toast('名称不匹配，操作取消',true);return;}
  try{await submitJob(`/api/vms/${encodeURIComponent(vm.id)}/stop`,'POST',{mode:'hard',confirm_data_loss:true});}catch(e){toast(e.message,true);}
}
async function deleteVm(vm){
 const name=prompt(`永久删除虚拟机「${vm.name}」？\n\n此操作将删除 D 盘对应的 VM 文件夹、所有虚拟磁盘和快照，无法恢复。\n必须先关机，不能有任务或锁。\n\n请输入完整虚拟机名称确认：`);
 if(name===null)return;
 if(name!==vm.name){toast('名称不匹配，已取消删除',true);return;}
 if(!confirm(`最后确认：永久删除「${vm.name}」及其目录全部文件？\n这不会删除黄金模板，但会清除该客户 VM 的 Windows、软件和快照。`))return;
 try{
  await submitJob(`/api/vms/${encodeURIComponent(vm.id)}`,'DELETE',{
   confirm_name:vm.name, confirm_permanent_delete:true
  });
  if($('vmDialog').open)$('vmDialog').close();navigate('jobs');
 }catch(e){toast('删除失败：'+e.message,true);}
}
async function openDiagnostics(vm) {
  const body=$('dialogBody');body.replaceChildren();
  $('dialogTitle').textContent=`${vm.name} · 状态诊断`;
  $('dialogSub').textContent='只读检查，不删除锁文件、不修改虚拟磁盘';
  body.append(el('p','snap-hint','正在检查 VMware 电源、VMX 文件占用及锁文件...'));
  if(!$('vmDialog').open)$('vmDialog').showModal();
  try {
    const d=await request(`/api/vms/${encodeURIComponent(vm.id)}/diagnostics`);
    const grid=el('div','detail-table');
    [['VMware 电源状态',statusName[d.power_state]||d.power_state],
     ['VMX 存在',d.vmx_exists?'是':'否'],
     ['VMX 可独占打开',d.vmx_probe_skipped?'未检测（电源/挂起保护）':d.vmx_exclusive_open?'是':'否'],
     ['锁标记',d.lock_markers.length?d.lock_markers.join(', '):'无'],
     ['挂起文件',d.suspend_files.length?d.suspend_files.join(', '):'无'],
     ['VMX 离线编辑',d.offline_vmx_edit_possible?'允许尝试':'不允许'],
     ['当前任务',d.active_jobs.length?d.active_jobs.join(', '):'无']]
      .forEach(([k,v])=>detailLine(grid,k,v));
    body.replaceChildren(grid,el('p','snap-hint',d.reason));
    if(d.warning)body.append(el('p','warning',d.warning));
    const row=el('div','snap-toolbar');
    row.append(btn('重新诊断','ghost',()=>openDiagnostics(vm)));
    if(d.power_state==='SUSPENDED'&&vm.state==='READY'){
      row.append(btn('恢复运行','primary',async()=>{
        await vmAction(vm,'start');
        if($('vmDialog').open)$('vmDialog').close();
      }));
    }
    body.append(row);
  } catch(e) {body.replaceChildren(el('p','warning','诊断失败：'+e.message));}
}
async function openProvision(vm) {
  $('dialogTitle').textContent=`${vm.name} · 首次配置`;
  $('dialogSub').textContent='独立 Windows 初始账户，仅供本机管理员操作';
  const body=$('dialogBody');body.replaceChildren(el('p','snap-hint','正在读取初始化状态...'));
  if(!$('vmDialog').open)$('vmDialog').showModal();
  try {
    const p=await request(`/api/vms/${encodeURIComponent(vm.id)}/unattend`);
    const grid=el('div','detail-table');
    let boot=null;
    try{boot=await request(`/api/vms/${encodeURIComponent(vm.id)}/firstboot-state`);}catch{}
    const verified=boot?.oobe_complete===true;
    [['配置介质已准备',p.prepared?'是':'否'],['应答 ISO 已挂载',p.media_attached?'是':'否'],
     ['Windows 计算机名',p.hostname||'未配置'],['初始账户',p.username||'未配置'],
     ['OOBE 状态',verified?'Guest Agent 验证通过':'未验证完成（不代表失败）'],
     ['Guest Agent 验证信息',boot?.oobe_evidence?.report?.stage||'无心跳 / 无验证标记']].forEach(([k,v])=>detailLine(grid,k,v));
    body.replaceChildren(grid,el('p','snap-hint',p.note));
    if(p.prepared){
      const actions=el('div','snap-toolbar');
      const reveal=btn('查看本机初始密码','ghost',async()=>{
        if(!confirm('只在可信 Windows 宿主机上查看此 VM 的独立初始密码。密码将显示在页面上，不要截图分享。继续？'))return;
        try{const s=await request(`/api/vms/${encodeURIComponent(vm.id)}/unattend/reveal-password`,'POST');
          const box=el('div','secret-view');box.append(el('strong','',s.username+' / '+s.hostname),el('code','',s.password));
          body.append(box);reveal.disabled=true;
        }catch(e){toast(e.message,true);}
      });
      actions.append(reveal);
      if(p.media_attached){actions.append(btn('关机后卸载应答 ISO','warn',async()=>{
        if(!confirm('请先确认该客户机已完成首次配置并完全关机。卸载后会删除 ISO 中的明文初始化密码，主机仍保留加密凭据。继续？'))return;
        try{await submitJob(`/api/vms/${encodeURIComponent(vm.id)}/unattend/cleanup`,'POST',{confirm_oobe_checked:true});$('vmDialog').close();navigate('jobs');}
        catch(e){toast(e.message,true);}
      },vm.power_state!=='POWERED_OFF'));}
      body.append(actions);
    }
  }catch(e){body.replaceChildren(el('p','warning','读取失败：'+e.message));}
}
async function repair(vm){if(!confirm(`仅当 ${vm.name} 因重复 VMware UUID/MAC 导致完整克隆失败，才可以修复；并确认其已关机。继续？`))return;
 try{await submitJob(`/api/vms/${encodeURIComponent(vm.id)}/repair-clone-identity`);}catch(e){toast(e.message,true);}}
function detailLine(grid,k,v){grid.append(el('div','',k),el('div','',v||'—'));}
async function snapshotAction(vm, snapshot, action) {
  const restore=action==='restore', operation=restore?'恢复':'删除';
  if(vm.power_state!=='POWERED_OFF'){
    toast('只有虚拟机完全关机后，才可以'+operation+'快照。请先手动关机。',true);return;
  }
  const note=restore?'恢复快照会丢弃此时间点之后的磁盘更改。':'删除快照将永久丢失该还原点，可能触发虚拟磁盘合并。';
  if(!confirm(`虚拟机：${vm.name}\n快照：${snapshot}\n\n${note}\n\n确认${operation}吗？`))return;
  const path=`/api/vms/${encodeURIComponent(vm.id)}/snapshots/${encodeURIComponent(snapshot)}`;
  try{
    await submitJob(restore?path+'/restore':path,restore?'POST':'DELETE',restore?{confirm_data_loss:true}:{});
    $('vmDialog').close();navigate('jobs');
  }catch(e){toast(e.message,true);}
}
async function openDetail(id) {
  state.viewingVm=id;
  let vm=state.vms.find(x=>x.id===id);
  if(!vm)return;
  try{vm=await request(`/api/vms/${encodeURIComponent(id)}`);}
  catch(e){toast('无法获取最新虚拟机状态：'+e.message,true);return;}
  $('dialogTitle').textContent=vm.name;
  $('dialogSub').textContent=vm.id;
  const body=$('dialogBody');body.replaceChildren();
  const grid=el('div','detail-table');
  [['客户标签',vm.customer_ref||'未设置'],['管理状态',vm.state],
   ['电源状态',statusName[vm.power_state]||vm.power_state],
   ['VMX 路径',vm.vmx_path],['模板 ID',vm.template_id]]
   .forEach(([a,b])=>detailLine(grid,a,b));
  body.append(grid);
  const busy=state.jobs.some(j=>j.vm_id===id && ['QUEUED','RUNNING'].includes(j.status));
  if(vm.state==='READY'){
    const ops=el('div','snap-toolbar');
    ops.append(btn('状态诊断','ghost small',()=>openDiagnostics(vm)));
    ops.append(btn('初始化信息','ghost small',()=>openProvision(vm)));
    if(vm.power_state==='POWERED_ON')ops.append(btn('强制关机','danger small',()=>hardStop(vm)));
    if(vm.power_state==='POWERED_OFF')ops.append(btn('删除虚拟机','danger small',()=>deleteVm(vm),busy));
    body.append(ops);
  }
  const section=el('section','detail-section');
  section.append(el('h3','','快照管理'));
  const toolRow=el('div','snap-toolbar');
  const offline=vm.state==='READY' && vm.power_state==='POWERED_OFF' && !busy;
  const createBtn=btn('＋ 新建快照','primary small',async()=>{
    if(!offline)return;
    const name=prompt('快照名称（英文、数字、下划线或连字符）');
    if(!name)return;
    if(!/^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$/.test(name)){toast('快照名格式不正确',true);return;}
    try{await submitJob(`/api/vms/${encodeURIComponent(id)}/snapshots`,'POST',{name});$('vmDialog').close();navigate('jobs');}
    catch(e){toast(e.message,true);}
  },true);
  if(vm.state==='READY')toolRow.append(createBtn);
  toolRow.append(btn('刷新快照','ghost small',()=>openDetail(id)));
  section.append(toolRow);
  if(vm.power_state==='POWERED_ON'){
    section.append(el('p','snap-hint','虚拟机正在运行：可以查看快照，但不能创建、恢复或删除快照。请先正常关闭 Windows，待显示“已关机”后再操作。'));
  }else if(vm.power_state==='SUSPENDED'){
    section.append(el('p','snap-hint','虚拟机已挂起：请恢复后正常关机，不能对挂起的 VM 操作快照。'));
  }else if(vm.power_state==='UNKNOWN'){
    section.append(el('p','snap-hint','当前无法确认电源状态，所有快照修改操作已禁止。'));
  }
  if(vm.state!=='READY'||busy){
    section.append(el('p','snap-hint','任务执行中或虚拟机尚未就绪。当前不查询快照，任务完成后点“刷新快照”。'));
  }else{
    try{
      const result=await request(`/api/vms/${encodeURIComponent(id)}/snapshots`);
      const count=result.count??result.snapshots.length, limit=result.limit??3;
      const cap=el('span','snap-count',`快照 ${count}/${limit}`);
      if(count>=limit)cap.classList.add('full');
      toolRow.prepend(cap);
      createBtn.textContent=`＋ 新建快照（${count}/${limit}）`;
      createBtn.disabled=!offline||count>=limit;
      createBtn.title=!offline?'必须先关机':count>=limit?'最多 3 个快照，请先删除旧快照':'';
      if(count>=limit)section.append(el('p','snap-hint','已达到每台最多 3 个快照的限制。请在关机状态下删除不再需要的旧快照，然后才能创建新的。'));
      if(!result.snapshots.length)section.append(el('p','muted','暂时没有快照'));
      result.snapshots.forEach(s=>{
        const row=el('div','snap-row'),acts=el('div','snap-actions');
        row.append(el('span','',s));
        const restoreBtn=btn('恢复','warn',()=>snapshotAction(vm,s,'restore'),!offline);
        const deleteBtn=btn('删除','danger',()=>snapshotAction(vm,s,'delete'),!offline);
        restoreBtn.title=deleteBtn.title=!offline?'需先完全关机':'';
        acts.append(restoreBtn,deleteBtn);row.append(acts);section.append(row);
      });
    }catch(e){section.append(el('p','snap-hint','目前无法读取快照：'+e.message+'。任务完成后可点击“刷新快照”。'));}
  }
  body.append(section);
  if(!$('vmDialog').open)$('vmDialog').showModal();
}
function openReinstall(vm){
  if(vm.state!=='READY'||vm.power_state!=='POWERED_OFF'||vm.suspend_reinstall_blocked){toast('尚未满足安全重装条件：请先确认关机及挂起文件状态',true);return;}
  state.reinstallVm=vm;
  $('reinstallTarget').textContent=`目标：${vm.name} · ${vm.customer_ref||'无客户标签'}`;
  $('reinstallTypedName').value='';$('reinstallEraseConfirm').checked=false;$('reinstallBackupConfirm').checked=false;
  $('reinstallUnattended').checked=false;$('reinstallScreen').checked=true;
  const select=$('reinstallTemplate');select.replaceChildren(new Option('选择已封存模板',''));
  state.templates.filter(t=>t.status==='SEALED_ATTESTED').forEach(t=>select.add(new Option(t.name,t.id)));
  select.value=state.defaultTemplate||vm.template_id;
  $('reinstallUnattended').checked=(state.templates.find(t=>t.id===select.value)?.oobe_bootstrap_version||0)>=2;
  $('reinstallDialog').showModal();
}
const softwareGuestUI={page:1,pageSize:18,expanded:new Set(),releaseExpanded:new Set()};

function renderGuestInventory(){
 const root=$('guestList');if(!root)return;
 const find=$('softwareGuestSearch').value.trim().toLocaleLowerCase();
 const filter=$('softwareGuestFilter').value;
 const guestMap=new Map(state.guests.map(g=>[g.vm_id,g]));
 const current=state.vms.map(vm=>{
   const guest=guestMap.get(vm.id);
   const registered=!!guest&&!guest.revoked_at;
   const online=registered&&!!guest.last_seen&&
     (Date.now()-new Date(guest.last_seen).getTime())<=5*60000;
   const appList=Object.entries(guest?.reported?.portable_software||{});
   return {vm,guest,registered,online,appList};
 }).filter(entry=>{
   const status=entry.online?'online':entry.registered?'offline':'unregistered';
   if(filter!=='all'&&filter!==status)return false;
   const apps=entry.appList.map(([name,meta])=>name+' '+(meta?.version_hint||'')).join(' ');
   const match=[entry.vm.name,entry.vm.customer_ref||'',entry.vm.id,
     ...(entry.guest?.reported?.ipv4_addresses||[]),apps].join(' ').toLocaleLowerCase();
   return !find||match.includes(find);
 });
 const pages=Math.max(1,Math.ceil(current.length/softwareGuestUI.pageSize));
 softwareGuestUI.page=Math.max(1,Math.min(softwareGuestUI.page,pages));
 const visible=current.slice((softwareGuestUI.page-1)*softwareGuestUI.pageSize,
                              softwareGuestUI.page*softwareGuestUI.pageSize);
 $('softwareGuestCount').textContent='筛选 '+current.length+' / '+state.vms.length+' 台'+
   ' · 在线 '+current.filter(e=>e.online).length+' 台';
 $('softwareGuestPage').textContent='第 '+softwareGuestUI.page+' / '+pages+' 页';
 $('softwareGuestPrev').disabled=softwareGuestUI.page<=1;
 $('softwareGuestNext').disabled=softwareGuestUI.page>=pages;
 root.replaceChildren();
 if(!state.capabilities?.guest_gateway_configured){
   root.append(el('p','warning','Guest 网关尚未正确配置，自动注册和版本上报可能无法工作。'));
 }
 if(!visible.length){root.append(el('p','muted','没有符合筛选条件的客户机'));return;}
 for(const {vm,guest,registered,online,appList} of visible){
   const details=el('details','software-guest-row');
   details.dataset.id=vm.id;details.open=softwareGuestUI.expanded.has(vm.id);
   details.addEventListener('toggle',()=>{
     if(details.open)softwareGuestUI.expanded.add(vm.id);
     else softwareGuestUI.expanded.delete(vm.id);
   });
   const summary=el('summary','software-guest-summary');
   summary.append(el('strong','software-guest-name',vm.name));
   const status=el('span','software-guest-state'+(online?' is-online':registered?' is-offline':' is-missing'),
     online?'在线':registered?'离线':'未注册');
   summary.append(status);
   summary.append(el('span','software-guest-brief','Agent '+
     (guest?.reported?.guest_agent||'未上报')+' · 软件 '+appList.length+' 项'));
   if(appList.length){
     const info=appList.slice(0,2).map(([name,value])=>name+' '+(value?.version_hint||'版本未知')).join(' · ');
     summary.append(el('span','software-guest-brief software-guest-version',info+
       (appList.length>2?' 等':'') ));
   }
   details.append(summary);
   const body=el('div','software-guest-details');
   const ip=(guest?.reported?.ipv4_addresses||[]).join('、')||'无';
   const wss=guest?.wss?.state||'never_connected';
   body.append(el('p','muted',
      '客户：'+(vm.customer_ref||'未设置')+' · IP：'+ip+' · WSS：'+wss+
      ' · 最后心跳：'+(guest?.last_seen?VMO_BJT(guest.last_seen):'无')));
   body.append(el('p','muted','VM ID：'+vm.id));
   if(appList.length){
     const apps=el('div','software-app-tags');
     appList.forEach(([name,item])=>{
       apps.append(el('span','software-app-tag',name+' · '+(item?.version_hint||'版本未知')+
         (item?.version_verified?'':'（未核验）')));
     });
     body.append(apps);
   }else body.append(el('p','muted','未发现可识别的软件目录；请核对 C:\\dsh 和 Guest Agent v2.0.25+'));
   details.append(body);root.append(details);
 }
}

function renderFleet(){
 renderGuestInventory();
 const guestMap=new Map(state.guests.map(x=>[x.vm_id,x]));
 const pack=$('packageList');
 if(!pack.contains(document.activeElement)){
   pack.replaceChildren();
   const releases=state.packages.filter(p=>p.product.startsWith('portable-p-'));
   if(!releases.length)pack.append(el('p','muted','暂时没有统一便携软件 ZIP 版本包。'));
   for(const p of releases){
     const profile=state.portableProfiles.find(x=>p.product==='portable-'+x.id);
     if(!profile)continue;
     // All online Agent 2.0.27+ clients, whether or not the program exists.
     const eligible=state.vms.filter(machine=>{
       const g=guestMap.get(machine.id);
       if(machine.state!=='READY'||!g||g.revoked_at||!g.last_seen||
          Date.now()-new Date(g.last_seen).getTime()>600000)return false;
       const ver=String(g.reported?.guest_agent||'').split('.').map(Number);
       return ver.length===3&&ver.every(Number.isInteger)&&
          (ver[0]>2||(ver[0]===2&&(ver[1]>0||ver[2]>=27)));
     });
     const installedCount=eligible.filter(machine=>{
       const report=guestMap.get(machine.id)?.reported?.portable_software||{};
       return Object.keys(report).some(x=>x.toLocaleLowerCase()===profile.folder.toLocaleLowerCase());
     }).length;
     const details=el('details','software-release');
     details.open=softwareGuestUI.releaseExpanded.has(p.id);
     details.addEventListener('toggle',()=>{
       if(details.open)softwareGuestUI.releaseExpanded.add(p.id);
       else softwareGuestUI.releaseExpanded.delete(p.id);
     });
     const summary=el('summary','software-guest-summary');
     summary.append(el('strong','software-guest-name',profile.display_name+' · '+p.version),
       el('span','software-guest-brief','目标 C:\\dsh\\'+profile.folder),
       el('span','software-guest-state'+(eligible.length?' is-online':' is-offline'),
          '可部署 '+eligible.length+' 台'),
       el('span','software-guest-state'+(profile.auto_deploy?' is-online':' is-offline'),
          profile.auto_deploy?'自动推送':'手动下发'));
     details.append(summary);
     const body=el('div','software-guest-details');
     body.append(el('p','muted','启动：'+profile.launcher+' · 版本来源：'+
       (p.version_source||'未知')+' · SHA-256：'+p.sha256.slice(0,24)+'…'));
     const autoRow=el('label','software-auto-control');
     const auto=document.createElement('input');auto.type='checkbox';
     auto.checked=Boolean(profile.auto_deploy);
     auto.setAttribute('aria-label','为 '+profile.display_name+' 自动推送到全部客户机');
     const autoText=el('span','',profile.auto_deploy?
       '自动推送已开启：新版本和新上线客户机将自动接收':'自动推送全部客户机（默认关闭）');
     auto.onchange=async()=>{
       const enabled=auto.checked;
       if(enabled&&!confirm('确定启用「'+profile.display_name+'」的自动推送？\n\n'+
         '当前在线的全部客户机将立即下发当前生效版本；以后上线的客户机也会自动接收。\n'+
         '同一版本每台只自动尝试一次，失败不会自动重试。\n'+
         '新上传的版本会取代旧生效版本。')){
         auto.checked=false;return;
       }
       auto.disabled=true;
       try{
         const result=await request('/api/fleet/portable/profiles/'+encodeURIComponent(profile.id)+
           '/auto-deploy','POST',{enabled,confirm_all:enabled});
         autoText.textContent=enabled?
           '自动推送已开启：新版本和新上线客户机将自动接收':'自动推送全部客户机（默认关闭）';
         toast(result.note+(enabled?' 本次已排队 '+result.queued+' 台；跳过 '+result.skipped+' 台。':''));
         await refresh();
       }catch(err){
         auto.checked=!enabled;toast('自动推送设置失败：'+err.message,true);
       }finally{auto.disabled=false;}
     };
     autoRow.append(auto,autoText);
     body.append(autoRow,el('p','muted','同一软件只保留一个当前生效的 ZIP 发布版本；历史版本仅供审计。'));
     const github=el('div','software-github-source');
     github.append(el('strong','','正式 Release 更新源（支持 HTTPS 镜像/代理）'));
     const urlLabel=el('label','','仓库地址（GitHub 或公开 HTTPS 镜像/代理地址）');
     const repoInput=el('input');repoInput.type='url';repoInput.maxLength=300;
     repoInput.value=profile.github_repo||'';
     repoInput.placeholder='https://git.wksite.cn/popowk/dsh-fish-soul-bridage';
     urlLabel.append(repoInput);
     const assetLabel=el('label','','ZIP 文件名（可留空自动选择唯一 ZIP）');
     const assetInput=el('input');assetInput.maxLength=180;assetInput.value=profile.github_asset||'';
     assetInput.placeholder='留空选唯一 ZIP；或填写 MyApp-v{version}-Windows-Full.zip';
     assetLabel.append(assetInput);
     const enabledRow=el('label','software-auto-control');
     const githubEnabled=document.createElement('input');githubEnabled.type='checkbox';
     githubEnabled.checked=Boolean(profile.github_enabled);
     enabledRow.append(githubEnabled,el('span','','开启自动检查最新正式 Release（每6小时）'));
     const status=el('p','muted','最近检查：'+(profile.github_last_checked||'未检查')+
       (profile.github_last_tag?' · '+profile.github_last_tag:'')+
       (profile.github_last_error?' · 失败：'+profile.github_last_error:
         profile.github_last_result?' · '+profile.github_last_result:''));
     const actions=el('div','software-release-actions');
     const save=btn('保存更新源','ghost small',async()=>{
       if(githubEnabled.checked&&!repoInput.value.trim())
         return toast('请先输入公开 HTTPS 仓库或镜像地址',true);
       if(githubEnabled.checked&&!confirm('自动检查会定期下载并验证 GitHub 正式 ZIP，然后替换服务器当前生效版本。\n若“自动推送全部客户机”也已打开，验证成功后还会给在线客户机下发新版本。确定开启？'))return;
       save.disabled=true;
       try{
         const result=await request('/api/fleet/portable/profiles/'+encodeURIComponent(profile.id)+
           '/github-source','POST',{url:repoInput.value.trim(),
             asset_name:assetInput.value.trim(),enabled:githubEnabled.checked});
         toast(result.note);save.blur();await refresh();
       }catch(error){toast('更新源保存失败：'+error.message,true)}
       finally{save.disabled=false;}
     });
     const check=btn('立即检查并验证','primary small',async()=>{
       if(!confirm('立即检查新正式 Release？校验成功后会更新服务器生效包，并按已开启的自动推送策略下发。'))return;
       check.disabled=true;status.textContent='正在下载和校验 GitHub Release…';
       try{
         const result=await request('/api/fleet/portable/profiles/'+encodeURIComponent(profile.id)+
           '/github-check','POST',{});
         status.textContent=(result.state==='published'?'发布成功':'当前无新版本')+
           ' · '+(result.tag||'')+
           (result.state==='published'?' · 已排队 '+(result.auto_queued||0)+' 台':'');
         toast(status.textContent);check.blur();await refresh();
       }catch(error){status.textContent='检查失败：'+error.message;toast(status.textContent,true)}
       finally{check.disabled=false;}
     });
     actions.append(save,check);
     github.append(urlLabel,el('p','muted','支持 https://镜像/owner/repo 或 https://代理/https://github.com/owner/repo；代理需提供兼容的 Release API 和 ZIP 下载服务。'),assetLabel,enabledRow,actions,status);
     body.append(github);

     const controls=el('div','software-release-actions');
     const select=el('select');select.setAttribute('aria-label','选择单台或全部客户机');
     select.append(new Option('请选择单台或全部',''));
     eligible.forEach(machine=>select.add(new Option(machine.name+'（'+(
       Object.keys(guestMap.get(machine.id)?.reported?.portable_software||{}).some(n=>
         n.toLocaleLowerCase()===profile.folder.toLocaleLowerCase())?'已安装':'首次安装'
     )+'）',machine.id)));
     if(eligible.length)select.add(new Option('全部在线客户机（'+eligible.length+' 台）','__ALL__'));
     const previous=state.packageSelections[p.id]||'';
     select.value=eligible.some(x=>x.id===previous)||previous==='__ALL__'?previous:'';
     select.addEventListener('change',()=>{state.packageSelections[p.id]=select.value;});
     controls.append(select,btn('下发软件','primary small',async()=>{
       if(!select.value)return toast('请选择一台或全部客户机',true);
       const all=select.value==='__ALL__';
       const target=all?'全部 '+eligible.length+' 台在线客户机':
         (eligible.find(x=>x.id===select.value)?.name||'指定客户机');
       if(!confirm('确认对 '+target+' 部署 '+profile.display_name+' '+p.version+'？\n\n'+
          '已安装：自动备份替换；先前在运行则恢复运行。\n'+
          '未安装：下载校验并首次解压到 C:\\dsh\\'+profile.folder+'，默认保持关闭。\n'+
          '离线、Guest 版本不足或正在更新的客户机将跳过。'))return;
       try{
         const result=await request('/api/fleet/portable/deploy','POST',{
           package_id:p.id,scope:all?'all':'one',
           vm_id:all?null:select.value,confirm:true,confirm_all:all
         });
         const failed=result.skipped||[];
         toast('已排队 '+result.queued.length+' / '+result.total+' 台'+
           (failed.length?'；跳过 '+failed.length+' 台，'+failed.slice(0,3)
             .map(x=>(state.vms.find(v=>v.id===x.vm_id)?.name||x.vm_id)+'：'+x.reason).join('；'):''),
           Boolean(failed.length));
         await refresh();
       }catch(e){toast('部署失败：'+e.message,true);}
     },!eligible.length));
     controls.append(btn('删除版本包','danger small',async()=>{
       if(!confirm('删除 '+profile.display_name+' '+p.version+' 的 ZIP 更新包？正在执行及绑定的任务将阻止删除。'))return;
       try{await request('/api/fleet/packages/'+encodeURIComponent(p.id),'DELETE');
         delete state.packageSelections[p.id];
         softwareGuestUI.releaseExpanded.delete(p.id);
         toast('版本包已删除');await refresh();
       }catch(e){toast(e.message,true);}
     }));
     body.append(controls);
     body.append(el('p','muted','当前在线可部署 '+eligible.length+' 台，其中已安装 '+installedCount+' 台，首次安装 '+(eligible.length-installedCount)+' 台。'));
     if(!eligible.length)body.append(el('p','muted','需要 Guest Agent v2.0.27+ 且最近 10 分钟有心跳。'));
     details.append(body);pack.append(details);
   }
 }
 const tasks=$('updateTaskList');tasks.replaceChildren();if(!state.updateTasks.length)tasks.append(el('p','muted','暂无更新任务'));
 for(const t of state.updateTasks){const vm=state.vms.find(x=>x.id===t.vm_id);let line=el('div','template-card');
   const stepLabels={starting:'开始更新',downloaded:'更新包已下载',staged:'已完成文件预备',stopped:'程序已停止',swapped:'已切换新版本',healthy:'按原运行状态恢复并通过进程检查',rolled_back_healthy:'更新失败，已恢复旧版本',recovery_required:'自动恢复失败，需人工处理',verified:'已完成验证'};
   const statusMessage=t.status==='QUEUED'?'等待 Guest Agent 领取':t.status==='CLAIMED'?'Guest 已领取，等待阶段回报':t.status==='RUNNING'?(stepLabels[t.phase]||'正在执行')+' · 最近报告 '+VMO_BJT(t.progress_at): (t.message||'已结束');
   line.append(el('div','',`${vm?.name||t.vm_id} · ${t.kind||'UPDATE'} · ${statusName[t.status]||t.status} · ${t.status==='VERIFY_REQUIRED'?'程序或插件命令已执行，但真实运行版本尚未核验。请查看应用上报版本。':statusMessage}`));
tasks.append(line);}
 renderPager('updatesPager',state.updatesPaged,page=>{state.updatePage=page;refresh();});
 const history=$('reinstallHistory');history.replaceChildren();if(!state.reinstalls.length)history.append(el('p','muted','暂无重装记录'));
 for(const x of state.reinstalls.slice(0,20)){const vm=state.vms.find(v=>v.id===x.vm_id);history.append(el('div','template-card',`${vm?.name||x.vm_id} · ${x.status} · ${x.backup_deleted_at?'旧备份已清理：'+VMO_BJT(x.backup_deleted_at):'旧系统备份：'+(x.backup_path||'未生成')}`));}
}
async function sealTemplate(t){
 if(!confirm(`你是否亲自确认：\n1. 已执行 Sysprep /generalize 并关机\n2. 已检查并清除客户凭据\n3. 模板已关机且没有挂起\n4. 这些是人工声明，不能代替系统自动证明？`))return;
 const operator=prompt('封存确认人（至少 2 个字符）：','local-admin');
 if(!operator||operator.trim().length<2)return;
 const ready=confirm(`是否已在未封存的 BUILD 副本执行【V1.2】Install-OOBE-Bootstrap.ps1、Seal-Win10Template.ps1 -UnattendedBootstrap，且 Sysprep 成功关机后未再启动模板？\n\n【确定】人工声明具备 Administrator V1.2 引导；【取消】作为普通模板登记。`);
 try{
  await request(`/api/templates/${encodeURIComponent(t.id)}/seal-attestation`,'POST',{
   operator:operator.trim(),sysprep_completed:true,credentials_reviewed:true,
   guest_is_shutdown:true,understood_not_cryptographic_proof:true,
   unattend_bootstrap_installed:ready,unattend_bootstrap_version:ready?2:0
  });toast('模板已封存；无人值守能力需新克隆实测');await refresh();
 }catch(e){toast(e.message,true);}
}

// Prevent back-forward cache from briefly restoring an old authenticated shell.
window.addEventListener('pagehide',()=>{
  const shell=document.getElementById('adminShell');
  if(shell)shell.hidden=true;
  VMOCloseEvents();
});
window.addEventListener('pageshow',event=>{
  if(event.persisted)window.location.reload();
});
window.addEventListener('DOMContentLoaded',async()=>{
 document.querySelectorAll('[data-page]').forEach(b=>b.addEventListener('click',()=>navigate(b.dataset.page)));
 document.querySelectorAll('[data-goto]').forEach(b=>b.addEventListener('click',()=>navigate(b.dataset.goto)));
 $('refreshBtn').addEventListener('click',refresh);$('refreshJobs').addEventListener('click',refresh);
 $('reinstallClose').addEventListener('click',()=>$('reinstallDialog').close());
 $('reinstallCancel').addEventListener('click',()=>$('reinstallDialog').close());
 $('reinstallForm').addEventListener('submit',async e=>{
   e.preventDefault();const vm=state.reinstallVm;if(!vm)return;
   if($('reinstallTypedName').value!==vm.name){toast('输入的虚拟机名称不匹配',true);return;}
   if(!confirm(`最后确认：将从黄金模板重新安装 ${vm.name}？旧 Windows 和快照不会进入新系统，旧副本会保留在回退备份区。`))return;
   if($('reinstallUnattended').checked && !(state.templates.find(t=>t.id===$('reinstallTemplate').value)?.oobe_bootstrap_version>=2))return toast('该模板不支持自动 OOBE，请选择已配置引导的新模板',true);
   try{await submitJob(`/api/vms/${encodeURIComponent(vm.id)}/reinstall`,'POST',{
     template_id:$('reinstallTemplate').value, confirm_name:vm.name,
     unattended_oobe:$('reinstallUnattended').checked,enable_screen:$('reinstallScreen').checked,
     confirm_erase_customer_data:$('reinstallEraseConfirm').checked,
     confirm_backup_disk_space:$('reinstallBackupConfirm').checked});
     $('reinstallDialog').close();navigate('jobs');
   }catch(err){toast('重装提交失败：'+err.message,true);}
 });
 $('softwareGuestSearch').addEventListener('input',()=>{softwareGuestUI.page=1;renderGuestInventory();});
 $('softwareGuestFilter').addEventListener('change',()=>{softwareGuestUI.page=1;renderGuestInventory();});
 $('softwareGuestPrev').addEventListener('click',()=>{if(softwareGuestUI.page>1){softwareGuestUI.page--;renderGuestInventory();}});
 $('softwareGuestNext').addEventListener('click',()=>{softwareGuestUI.page++;renderGuestInventory();});
 $('portableUploadForm').addEventListener('submit',async ev=>{
   ev.preventDefault();
   const file=$('portableZip').files[0],name=$('portableName').value.trim(),
     launcher=$('portableLauncher').value.trim(),
     version=$('portableVersion').value.trim(),
     versionFile=$('portableVersionFile').value.trim();
   if(!file||!file.name.toLowerCase().endsWith('.zip'))return toast('请上传 ZIP 压缩包',true);
   if(file.size>512*1024*1024)return toast('ZIP 最大 512 MB',true);
   if(!name||!launcher)return toast('软件名称和启动文件不可为空',true);
   if(versionFile&&(/^[a-zA-Z]:[\\/]/.test(versionFile)||versionFile.startsWith('/')||versionFile.includes('..'))){
     return toast('版本文件只填写压缩包内的相对文件名（如 VERSION），不要填 C:\\dsh 安装目录',true);
   }
   if(!confirm('确认登记软件 ZIP？\n软件：'+name+'\n启动文件：'+launcher+
     '\nZIP：'+file.name+'\n\n上传只会登记版本，不会自动部署。'))return;
   const button=$('portableUploadForm').querySelector('button[type="submit"]');
   button.disabled=true;button.textContent='正在校验 ZIP 并登记…';
   try{
     const query=new URLSearchParams({name,launcher,version_override:version,version_file:versionFile});
     const response=await fetch('/api/fleet/portable/packages?'+query,{
       method:'POST',credentials:'same-origin',
       headers:{'X-UI-CSRF':state.csrf,'Content-Type':'application/zip'},body:file
     });
     const result=await response.json().catch(()=>({}));
     if(!response.ok)throw Error(result.detail||'上传失败 HTTP '+response.status);
     $('portableUploadForm').reset();
     const autoInfo=result.auto_deploy||{};
     toast((result.already_active?'相同 ZIP 已生效，无需重复上传。':'已登记 '+result.profile.display_name+
       ' · C:\\dsh\\'+result.profile.folder+' · '+result.profile.version+'. ')+
       (result.profile.auto_deploy?
         '自动推送开启：已排队 '+(autoInfo.queued_count||0)+' 台，跳过 '+
           (autoInfo.skipped_count||0)+' 台。':
         '自动推送关闭，可在软件版本库选择单台或全部下发。'));
     await refresh();
   }catch(error){toast('软件包校验或登记失败：'+error.message,true);}
   finally{button.disabled=false;button.textContent='上传并登记软件更新';}
 });
 $('cloneForm').addEventListener('submit',async ev=>{ev.preventDefault();const name=$('cloneName').value.trim(),template_id=$('cloneTemplate').value,customer_ref=$('cloneCustomer').value.trim()||null;
  const autoStart=$('cloneAutoStart').checked,screen=$('cloneEnableScreen').checked,unattended=$('cloneUnattended').checked;
  if(unattended && !(state.templates.find(t=>t.id===template_id)?.oobe_bootstrap_version>=2))return toast('所选模板未安装 V1.2 Administrator OOBE 引导，请选择新版封存模板或取消无人值守',true);
  if(!confirm(`确认从已封存模板完整克隆「${name}」？\n将占用新的虚拟磁盘空间。${unattended?'将创建每台独立 OOBE ISO 与随机密码；Windows 是否读取 ISO 需要实机验证。':''}${autoStart?'完成后将自动开机。':'完成后保持关机。'}`))return;
  try{await submitJob('/api/vms','POST',{name,template_id,customer_ref,start_after_clone:autoStart,enable_screen:screen,unattended_oobe:unattended});$('cloneName').value='';navigate('jobs');}catch(e){toast(e.message,true);}
 });
 function templateBuildModeChanged(){
   const clone=$('templateBuildMode').value==='clone';
   $('templateBuildIsoLabel').hidden=clone;
   $('templateBuildSourceLabel').hidden=!clone;
   $('templateBuildIso').required=!clone;
   $('templateBuildSource').required=clone;
 }
 $('templateBuildMode').addEventListener('change',templateBuildModeChanged);
 templateBuildModeChanged();
 $('templateBuildForm').addEventListener('submit',async ev=>{
   ev.preventDefault();
   const name=$('templateBuildName').value.trim(),mode=$('templateBuildMode').value;
   const payload={name,mode,
      iso_path:mode==='iso'?$('templateBuildIso').value.trim():'',
      source_template_id:mode==='clone'?$('templateBuildSource').value:'',
      memory_mb:Number($('templateBuildMemory').value),
      cpus:Number($('templateBuildCpus').value),
      disk_gb:Number($('templateBuildDisk').value)};
   if(!confirm('创建独立制作虚拟机 '+name+'？\n'+
      (mode==='iso'?'使用 ISO 创建新 Windows 虚拟磁盘。':'从已封存模板完整克隆，不修改来源文件。')+
      '\n制作目录：'+state.templateBuildRoot+'\n不会覆盖已有正式模板或客户机。'))return;
   const button=$('templateBuildForm').querySelector('button[type="submit"]');
   button.disabled=true;
   try{
     await request('/api/template-builds','POST',payload);
     toast('模板制作任务已排队');$('templateBuildName').value='';
     await refresh();
   }catch(e){toast('制作任务创建失败：'+e.message,true);}
   finally{button.disabled=false;}
 });
 $('scanTemplatesBtn').addEventListener('click',scanTemplates);
 $('templateForm').addEventListener('submit',async ev=>{ev.preventDefault();try{await request('/api/templates','POST',{name:$('templateName').value.trim(),vmx_path:$('templatePath').value.trim()});$('templateForm').reset();toast('模板已登记，还需要完成封存确认');await refresh();}catch(e){toast(e.message,true);}});
 $('loginForm').addEventListener('submit',async ev=>{ev.preventDefault();$('loginError').textContent='';try{let r=await request('/ui/login','POST',{username:'admin',password:$('loginKey').value});state.csrf=r.csrf;$('loginKey').value='';setLogin(false);await refresh();VMOConnectEvents();}catch(e){$('loginError').textContent=e.message;}});
 $('logoutBtn').addEventListener('click',async()=>{
   const csrf=state.csrf;
   $('adminShell').hidden=true; VMOCloseEvents();
   try {
     const response=await fetch('/ui/logout',{method:'POST',credentials:'same-origin',headers:{'X-UI-CSRF':csrf}});
     if(!response.ok && response.status!==401)throw Error('HTTP '+response.status);
     state.csrf=''; window.location.replace('/');
   } catch(e) { toast('退出登录未完成，请重试：'+e.message,true);$('adminShell').hidden=false; }
 });
 try{let r=await request('/ui/session');state.csrf=r.csrf;setLogin(false);await refresh();VMOConnectEvents();
    const requested=new URLSearchParams(window.location.search).get('page');
    if(requested&&pages[requested])navigate(requested);
  }catch(e){setLogin(true);}
 setInterval(()=>{if(!document.hidden&&state.csrf&&state.activePage!=='remote'&&state.activePage!=='clients')refresh();},7000);
});

// Administrator credentials/API keys: only the active settings dialog refreshes.
(function(){
 const dialog=$('accountDialog');if(!dialog)return;
 $('accountSettingsBtn').onclick=async()=>{dialog.showModal();await loadKeys();};
 $('accountClose').onclick=()=>{dialog.close();$('apiKeySecret').value='';$('apiKeyNewSecret').hidden=true;};
 async function loadKeys(){
  const data=await request('/api/admin/api-keys');const wrap=$('apiKeysList');wrap.replaceChildren();
  data.forEach(k=>{const row=el('div','template-card'),info=el('div');info.append(el('strong','',k.name),el('small','',`${k.scope} · ${k.revoked_at?'已撤销':'有效'} · ${VMO_BJT(k.created_at*1000)}`));row.append(info);
   if(!k.revoked_at)row.append(btn('撤销','danger small',async()=>{if(!confirm('撤销后使用该密钥的集成立即失效，确认？'))return;await request('/api/admin/api-keys/'+encodeURIComponent(k.id),'DELETE');await loadKeys();}));wrap.append(row);});
 }
 $('apiKeyCreateForm').onsubmit=async ev=>{ev.preventDefault();try{const r=await request('/api/admin/api-keys','POST',{name:$('apiKeyName').value,scope:$('apiKeyScope').value});$('apiKeySecret').value=r.token;$('apiKeyNewSecret').hidden=false;await loadKeys();}catch(e){toast(e.message,true);}};
 $('apiKeyCopyBtn').onclick=()=>navigator.clipboard.writeText($('apiKeySecret').value).then(()=>toast('已复制')).catch(()=>toast('请手动选择文本复制',true));
 $('passwordChangeForm').onsubmit=async ev=>{ev.preventDefault();try{await request('/ui/change-password','POST',{current_password:$('passwordCurrent').value,new_password:$('passwordNew').value});window.location.replace('/');}catch(e){toast(e.message,true);}};
})();

;

/* ===== business-admin.js ===== */
'use strict';
window.VMOClients=(()=>{
  const $=id=>document.getElementById(id);
  let products=[],services=[],editingProduct=null;
  function cancelEdit(){
    editingProduct=null;
    $('bizProductRef').readOnly=false;
    $('bizProductForm').reset();
    $('bizProductSave').textContent='保存商品映射';
    $('bizProductCancel').hidden=true;
    $('bizProductMode').textContent='选择一个商品映射进行修改；保留旧实例原模板；如果原模板失效，客户重装会使用该商品当前已封存模板。';
  }
  function editMapping(x){
    editingProduct=x.product_ref;
    $('bizProductRef').value=x.product_ref;
    $('bizProductRef').readOnly=true;
    $('bizTemplate').value=x.template_id;
    $('bizAllowUpdates').checked=Boolean(x.allow_updates);
    $('bizProductSave').textContent='保存模板修改';
    $('bizProductCancel').hidden=false;
    $('bizProductMode').textContent='正在修改商品 '+x.product_ref+'；已开通客户保留原模板，原模板失效后重装可回退到该商品的新封存模板。';
    $('bizProductForm').scrollIntoView({behavior:'smooth',block:'center'});
  }
  async function deleteMapping(x){
    const count=services.filter(s=>s.product_ref===x.product_ref).length;
    const warning=count?`此商品已有 ${count} 个历史/现有客户实例。删除不会删除它们的 VM，但商品更新权限可能受影响。\n`:'';
    if(!confirm(`确认删除商品 ${x.product_ref} 的黄金模板绑定？\n${warning}此操作不会删除黄金模板文件；新订单将无法用该商品开通，重新绑定后才能恢复。`))return;
    try{
      const result=await request('/api/business/admin/products/'+encodeURIComponent(x.product_ref),'DELETE');
      if(editingProduct===x.product_ref)cancelEdit();
      await refresh();
      toast(`绑定已删除；保留 ${result.preserved_services||0} 个已有客户实例`);
    }catch(e){toast(e.message,true);}
  }
  async function refresh(){if(!state.csrf)return;
    try{[products,services]=await Promise.all([request('/api/business/admin/products'),request('/api/business/admin/services')]);render();window.VMOBizUI?.metrics({services,products});window.VMOSelfService?.refresh(products);}
    catch(e){$('bizServices').textContent='加载失败：'+e.message;}
  }
  function render(){const p=$('bizProducts'),s=$('bizServices');p.replaceChildren();s.replaceChildren();
    const keep={template:$('bizTemplate').value,vm:$('bizExistingVm').value,product:$('bizProductForBind').value};
    const formFocused=[$('bizProductRef'),$('bizTemplate'),$('bizExistingVm'),$('bizProductForBind')].includes(document.activeElement);
    if(!formFocused){
    $('bizTemplate').replaceChildren(new Option('选择封存模板',''));
    for(const t of state.templates.filter(x=>x.status==='SEALED_ATTESTED'))$('bizTemplate').add(new Option(t.name,t.id));
    $('bizExistingVm').replaceChildren(new Option('选择现有就绪 VM',''));
    for(const vm of state.vms.filter(x=>x.state==='READY'))$('bizExistingVm').add(new Option(vm.name,vm.id));
    $('bizProductForBind').replaceChildren(new Option('选择商品映射',''));
    }
    for(const x of products){
      if(!formFocused)$('bizProductForBind').add(new Option(x.product_ref,x.product_ref));
      const r=el('div','template-card'),info=el('div'),act=el('div','snap-toolbar');
      const mapped=state.templates.find(t=>t.id===x.template_id);
      info.append(el('strong','',x.product_ref),el('small','',`黄金模板：${mapped?.name||x.template_id} · 客户软件更新：${x.allow_updates?'允许':'禁用'}`));
      info.append(el('small','',`现有服务实例：${services.filter(s=>s.product_ref===x.product_ref).length} · 原模板失效时允许同商品安全回退`));
      const provisioning=services.some(s=>s.product_ref===x.product_ref && s.status==='PROVISIONING');
      const remove=btn(provisioning?'开通中不可删除':'删除绑定','danger small',()=>deleteMapping(x),provisioning);
      if(provisioning)remove.title='该商品仍有开通中的订单，请等待订单结束后再删除绑定';
      act.append(btn('修改模板','ghost small',()=>editMapping(x)),remove);
      r.append(info,act);p.append(r);
    }
    if(!formFocused){
      if([...$('bizTemplate').options].some(o=>o.value===keep.template))$('bizTemplate').value=keep.template;
      if([...$('bizExistingVm').options].some(o=>o.value===keep.vm))$('bizExistingVm').value=keep.vm;
      if([...$('bizProductForBind').options].some(o=>o.value===keep.product))$('bizProductForBind').value=keep.product;
    }
    if(!products.length)p.append(el('p','muted','尚未设置思博商品与黄金模板的绑定。'));
    const needle=($('bizServiceSearch')?.value||'').trim().toLowerCase();
    const filter=($('bizStatusFilter')?.value||'');
    const visibleServices=services.filter(x=>(!needle||[x.service_ref,x.customer_ref,x.product_ref].some(v=>String(v||'').toLowerCase().includes(needle)))&&(!filter||x.status===filter));
    for(const x of visibleServices){const r=el('div','template-card'),info=el('div');info.append(el('strong','',`思博实例 ${x.service_ref}`),el('small','',`客户 ${x.customer_ref} · 商品 ${x.product_ref} · ${x.status}`),el('small','',`到期：${x.expires_at?new Date(x.expires_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'旧服务未设置'} · 到期关机：${x.expiry_status||'未配置'}`));
      if(x.expiry_status==='ENFORCING' && x.expiry_soft_requested_at){
        const when=new Date((x.expiry_soft_requested_at+600)*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
        info.append(el('small','',`已请求软关机；如仍在运行，最早于 ${when}（北京时间）强制关机`));
      }
      const act=el('div','snap-toolbar');
      act.append(btn('调整额度','ghost small',()=>{
        window.VMOBizUI.show('settings');
        const field=$('customerLimitService');
        field.value=x.service_ref;
        $('customerLimitLoad').click();
        requestAnimationFrame(()=>{field.scrollIntoView({behavior:'smooth',block:'center'});field.focus({preventScroll:true});});
      }));
      act.append(btn('本机客户预览','ghost small',async()=>{try{const t=await request(`/api/business/admin/services/${encodeURIComponent(x.service_ref)}/test-ticket`,'POST');
        const form=document.createElement('form');form.method='POST';form.action=t.action;form.target='_blank';form.enctype='application/x-www-form-urlencoded';const hidden=document.createElement('input');hidden.type='hidden';hidden.name='ticket';hidden.value=t.ticket;form.append(hidden);document.body.append(form);form.submit();form.remove();}catch(e){toast(e.message,true);}}));
      act.append(btn('修改到期时间','ghost small',async()=>{
        const previous=x.expires_at?new Date(x.expires_at).toLocaleString('sv-SE',{timeZone:'Asia/Shanghai',hour12:false}).replace(' ','T').slice(0,16):'';
        const next=prompt('输入新的北京时间（格式 YYYY-MM-DDTHH:mm），续费不会自动开机：',previous);
        if(next===null)return;
        if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(next)){toast('时间格式错误',true);return;}
        if(!confirm('确认变更服务 '+x.service_ref+' 的到期时间？'))return;
        try{await request('/api/business/admin/services/'+encodeURIComponent(x.service_ref)+'/expiry','PUT',{
          expires_at:new Date(next+':00+08:00').toISOString().replace('.000Z','Z'),expiry_action:'FORCE_POWER_OFF',expiry_policy_version:1
        });await refresh();toast('已保存新的到期时间');}catch(e){toast(e.message,true);}
      }));
      r.append(info,act);s.append(r);}
    if(!visibleServices.length)s.append(el('p','muted',services.length?'没有匹配的客户服务实例。':'暂无思博客户服务。请先建立商品映射并通过思博插件开通。'));
  }
  window.addEventListener('DOMContentLoaded',()=>{
    // Default to 30 days from today, shown in Beijing rather than the browser time zone.
    const future=new Date(Date.now()+30*86400*1000);
    $('bizExpiresAt').value=future.toLocaleString('sv-SE',{timeZone:'Asia/Shanghai',hour12:false}).replace(' ','T').slice(0,16);
    $('bizProductCancel').onclick=cancelEdit;
    $('bizProductForm').onsubmit=async e=>{e.preventDefault();try{
      await request('/api/business/admin/products','PUT',{product_ref:editingProduct||$('bizProductRef').value.trim(),template_id:$('bizTemplate').value,allow_updates:$('bizAllowUpdates').checked});
      cancelEdit();toast('思博商品映射已保存（原模板失效时可安全回退）');await refresh();
    }catch(ex){toast(ex.message,true);}};
    $('bizBindForm').onsubmit=async e=>{e.preventDefault();try{await request('/api/business/admin/bind-existing','POST',{service_ref:$('bizServiceRef').value.trim(),customer_ref:$('bizCustomerRef').value.trim(),product_ref:$('bizProductForBind').value,vm_id:$('bizExistingVm').value,
     expires_at:new Date($('bizExpiresAt').value+':00+08:00').toISOString().replace('.000Z','Z'),
     expiry_action:'FORCE_POWER_OFF',expiry_policy_version:1});toast('迁移实例绑定成功');await refresh();}catch(ex){toast(ex.message,true);}};
    $('bizRefresh').onclick=refresh;
    $('bizServiceSearch').addEventListener('input',render);
    $('bizStatusFilter').addEventListener('change',render);
  });
  return {refresh,get services(){return services;}};
})();

;

/* ===== biz-workspace.js ===== */
// Independent view switching; all existing form IDs and backend contracts remain intact.
'use strict';
window.VMOBizUI=(()=>{
 const $=id=>document.getElementById(id);
 let current='services';
 function show(name){
   if(!['services','products','actions','settings','bind'].includes(name))return;
   current=name;
   document.querySelectorAll('[data-biz-tab]').forEach(b=>{
     const selected=b.dataset.bizTab===name;
     b.classList.toggle('active',selected);
     b.setAttribute('aria-selected',String(selected));
   });
   document.querySelectorAll('[data-biz-view]').forEach(v=>v.classList.toggle('active',v.dataset.bizView===name));
 }
 function metrics({services,products,actions}={}){
   if(Array.isArray(services)){
     $('bizMetricServices').textContent=services.length;
     $('bizMetricActive').textContent=services.filter(x=>['ACTIVE','READY','RUNNING'].includes(x.status)).length;
   }
   if(Array.isArray(products))$('bizMetricProducts').textContent=products.length;
   if(Array.isArray(actions))$('bizMetricActions').textContent=actions.length;
 }
 window.addEventListener('DOMContentLoaded',()=>{
   document.querySelectorAll('[data-biz-tab]').forEach(b=>b.addEventListener('click',()=>show(b.dataset.bizTab)));
   $('bizGoBind').addEventListener('click',()=>show('bind'));
   $('bizRefreshTop').addEventListener('click',()=>window.VMOClients?.refresh());
 });
 return {show,metrics,get current(){return current;}};
})();

;

/* ===== self-service-admin.js ===== */
'use strict';
window.VMOSelfService=(()=>{
 const $=id=>document.getElementById(id);
 let catalog=[];
 let settingsLoaded=false;
 const commandLimit=()=>$('selfShell').value==='cmd'?8000:12000;
 function commandCount(){
   const input=$('selfCommand'),limit=commandLimit(),count=input.value.length;
   input.maxLength=limit;
   const label=$('selfCommandCount');
   label.textContent=count.toLocaleString('zh-CN')+' / '+limit.toLocaleString('zh-CN')+
     ' 字符（'+($('selfShell').value==='cmd'?'CMD':'PowerShell')+'）';
   label.classList.toggle('remote-count-error',count>limit);
 }
 function kindChange(){const isUpdate=$('selfKind').value==='SOFTWARE_UPDATE';$('selfCommandFields').hidden=isUpdate;$('selfUpdateFields').hidden=!isUpdate;commandCount();}
 function reset(){ $('selfActionForm').reset();$('selfEditId').value='';$('selfEnabled').checked=false;kindChange();$('selfEditorMode').textContent='新建一项客户可执行操作';$('selfSave').textContent='保存操作';render();}
 function prepare(){
   const product=$('selfProduct'),old=product.value;
   if(document.activeElement!==product){product.replaceChildren(new Option('选择商品',''));
     for(const p of (window.VMOSelfService.products||[]))product.add(new Option(p.product_ref,p.product_ref));
     if([...product.options].some(o=>o.value===old))product.value=old;}
   const packs=$('selfPackage'),oldPack=packs.value;
   if(document.activeElement!==packs){packs.replaceChildren(new Option('选择已审核的 ZIP 更新包',''));
     for(const p of (state.packages||[]))packs.add(new Option(p.product+' · '+p.version,p.id));
     if([...packs.options].some(o=>o.value===oldPack))packs.value=oldPack;}
 }
 async function loadCustomerSettings(){
   try{const data=await request('/api/business/admin/customer-settings');
     const d=data.defaults;
     $('customerDefaultSnapshots').value=d.snapshot_limit;
     $('customerDefaultRestores').value=d.monthly_restore_limit;
     $('customerDefaultReinstalls').value=d.monthly_reinstall_limit;
     const a=data.announcement;
     $('customerAnnouncementBody').value=a?.body||'';
     updateAnnouncementPreview();
     $('customerAnnouncementHours').value=a?.repeat_hours||24;
     $('customerAnnouncementEnabled').checked=!!a;
   }catch(e){toast('读取公告与额度失败：'+e.message,true);}
 }
 function updateAnnouncementPreview(){const text=$('customerAnnouncementBody').value.replace(/\r\n?/g,'\n');$('customerAnnouncementPreview').textContent=text||'公告内容将显示在这里';}
 function quotaValues(prefix){return {
   snapshot_limit:Number($(prefix+'Snapshots').value),
   monthly_restore_limit:Number($(prefix+'Restores').value),
   monthly_reinstall_limit:Number($(prefix+'Reinstalls').value)
 };}
 async function refresh(preloadedProducts){
   if(!state.csrf)return;
   if(!settingsLoaded){await loadCustomerSettings();settingsLoaded=true;}
   try{const [actions,products,executions]=await Promise.all([request('/api/business/admin/self-actions'),Array.isArray(preloadedProducts)?Promise.resolve(preloadedProducts):request('/api/business/admin/products'),request('/api/business/admin/self-executions')]);
     catalog=actions;window.VMOSelfService.products=products;prepare();render();renderExecutions(executions);$('selfTotalCount').textContent=actions.length;$('selfEnabledCount').textContent=actions.filter(a=>a.enabled).length;$('selfPendingCount').textContent=executions.filter(a=>['UNKNOWN','TIMED_OUT','VERIFY_REQUIRED'].includes(a.status)).length;window.VMOBizUI?.metrics({services:window.VMOClients?.services||[],products,actions});}
   catch(e){$('selfActionList').textContent='加载自助操作失败：'+e.message;}
 }
 function render(){
   const root=$('selfActionList');root.replaceChildren();
   const search=$('selfActionSearch').value.trim().toLowerCase(),filter=$('selfActionFilter').value;
   const visible=catalog.filter(a=>(filter==='all'||(filter==='enabled')===!!a.enabled)&&(!search||[a.label,a.description,a.product_ref].some(x=>String(x||'').toLowerCase().includes(search))));
   for(const a of visible){
     const row=el('article','self-action-card'+($('selfEditId').value===a.id?' is-editing':''));
     const top=el('div','self-action-card-top'),info=el('div'),status=el('span','self-status'+(a.enabled?' active':''),a.enabled?'已启用':'未启用');
     const desc=a.kind==='COMMAND'?(a.shell+' · '+({wait:'等待结果',detached:'后台启动',interactive:'桌面运行'}[a.run_mode]||a.run_mode)):'更新包 · '+(state.packages.find(p=>p.id===a.package_id)?.version||'包已移除');
     info.append(el('strong','',a.label),el('small','',a.product_ref+' · '+desc+' · 排序 '+a.sort_order));
     if(a.description)info.append(el('small','',a.description));
     top.append(info,status);
     const ops=el('div','self-action-actions');
     ops.append(btn('编辑','ghost small',()=>{
       for(const [id,val] of [['selfEditId',a.id],['selfProduct',a.product_ref],['selfLabel',a.label],['selfDescription',a.description],['selfKind',a.kind],['selfShell',a.shell],['selfCommand',a.command],['selfRunMode',a.run_mode],['selfCwd',a.cwd],['selfTimeout',a.timeout_seconds],['selfPackage',a.package_id],['selfInstallDir',a.install_dir],['selfSortOrder',a.sort_order??100]])$(id).value=val??'';
       $('selfEnabled').checked=!!a.enabled;kindChange();$('selfEditorMode').textContent='正在编辑 · '+a.label;$('selfSave').textContent='保存修改';render();$('selfActionForm').scrollIntoView({behavior:'smooth',block:'start'});$('selfLabel').focus({preventScroll:true});
     }));
     ops.append(btn(a.enabled?'停用':'启用','ghost small',async()=>{try{await request('/api/business/admin/self-actions/'+encodeURIComponent(a.id)+'/enabled','PUT',{enabled:!a.enabled});await refresh();toast('操作状态已更新');}catch(e){toast(e.message,true);}}));
     ops.append(btn('删除','danger small',async()=>{if(!confirm('删除「'+a.label+'」？已执行任务记录会保留。'))return;try{await request('/api/business/admin/self-actions/'+encodeURIComponent(a.id),'DELETE');if($('selfEditId').value===a.id)reset();await refresh();toast('操作已删除');}catch(e){toast(e.message,true);}}));
     row.append(top,ops);root.append(row);
   }
   if(!visible.length)root.append(el('p','self-empty',catalog.length?'没有符合筛选条件的操作。':'尚未配置操作。请在右侧创建第一项。'));
 }
 function renderExecutions(items){
   const root=$('selfExecutionList');root.replaceChildren();
   for(const a of items){
     const row=el('div','self-execution-row'),info=el('div'),ops=el('div','snap-toolbar');
     const label=catalog.find(x=>x.id===a.action_id)?.label||'已删除的操作';
     const status={UNKNOWN:'结果未知·需核对',TIMED_OUT:'超时·需核对',VERIFY_REQUIRED:'待核验',SUCCEEDED:'执行成功',FAILED:'执行失败',RUNNING:'执行中',QUEUED:'排队中',CLAIMED:'已接收',RESERVING:'准备下发',ACKNOWLEDGED:'管理员已确认'}[a.status]||a.status;
     info.append(el('strong','',label+' · '+status),el('small','',`实例 ${a.service_ref} · ${VMO_BJT(a.updated_at)}`));
     if(['UNKNOWN','TIMED_OUT','VERIFY_REQUIRED'].includes(a.status)){
       ops.append(btn('标记已核对','ghost small',async()=>{
         if(!confirm('确认已检查对应 VM 的进程、应用版本和 Agent 日志，明确当前任务不需要重试？'))return;
         try{await request('/api/business/admin/self-executions/'+encodeURIComponent(a.request_id)+'/acknowledge','POST',
           {reviewed_guest_state:true});toast('记录已人工确认；没有重复执行命令');await refresh();}
         catch(e){toast(e.message,true);}
       }));
     }
     row.append(info,ops);root.append(row);
   }
   if(!items.length)root.append(el('p','self-empty','暂无执行记录。'));
 }
 window.addEventListener('DOMContentLoaded',()=>{
   $('customerAnnouncementForm').onsubmit=async e=>{e.preventDefault();
     try{await request('/api/business/admin/customer-announcement','POST',{body:$('customerAnnouncementBody').value,repeat_hours:Number($('customerAnnouncementHours').value),enabled:$('customerAnnouncementEnabled').checked});toast('公告设置已发布，新版本会立即向客户展示');await loadCustomerSettings();}catch(ex){toast(ex.message,true);}
   };
   $('customerAnnouncementBody').addEventListener('input',updateAnnouncementPreview);
   $('customerDefaultsForm').onsubmit=async e=>{e.preventDefault();
     try{await request('/api/business/admin/customer-settings/defaults','PUT',quotaValues('customerDefault'));toast('默认额度已更新');}catch(ex){toast(ex.message,true);}
   };
   $('customerLimitLoad').onclick=async()=>{
     const ref=$('customerLimitService').value.trim();if(!ref)return toast('填写业务实例 ID',true);
     try{const x=await request('/api/business/admin/customer-settings/services/'+encodeURIComponent(ref));
       $('customerLimitSnapshots').value=x.limits.snapshot_limit;
       $('customerLimitRestores').value=x.limits.monthly_restore_limit;
       $('customerLimitReinstalls').value=x.limits.monthly_reinstall_limit;
       $('customerLimitUsage').textContent=`${x.usage.month}：已恢复 ${x.usage.restore_used} 次，已重装 ${x.usage.reinstall_used} 次`;
     }catch(ex){toast(ex.message,true);}
   };
   $('customerServiceLimitsForm').onsubmit=async e=>{e.preventDefault();
     const ref=$('customerLimitService').value.trim();
     try{await request('/api/business/admin/customer-settings/services/'+encodeURIComponent(ref),'PUT',quotaValues('customerLimit'));toast('客户额度已更新');$('customerLimitLoad').click();}catch(ex){toast(ex.message,true);}
   };
   $('selfKind').onchange=kindChange;
   $('selfShell').onchange=commandCount;
   $('selfCommand').oninput=commandCount;
   commandCount();
   $('selfActionSearch').oninput=render;
   $('selfActionFilter').onchange=render;
   $('selfRefresh').onclick=()=>refresh();
   $('selfReset').onclick=reset;
   $('selfActionForm').onsubmit=async e=>{e.preventDefault();
     const isUpdate=$('selfKind').value==='SOFTWARE_UPDATE';
     const body={id:$('selfEditId').value||null,product_ref:$('selfProduct').value,label:$('selfLabel').value.trim(),
       description:$('selfDescription').value.trim(),kind:$('selfKind').value,enabled:$('selfEnabled').checked,
       shell:$('selfShell').value,command:isUpdate?'':$('selfCommand').value,
       run_mode:$('selfRunMode').value,cwd:isUpdate?'':$('selfCwd').value.trim(),
       timeout_seconds:Number($('selfTimeout').value||60),package_id:isUpdate?$('selfPackage').value:'',
       install_dir:isUpdate?$('selfInstallDir').value.trim():'',sort_order:Number($('selfSortOrder').value||0)};
     if(!body.product_ref)return toast('请选择思博商品',true);
     if(!isUpdate && (!body.command.length || body.command.length>commandLimit()))
       return toast('命令长度无效：'+(body.shell==='cmd'?'CMD 最多 8,000':'PowerShell 最多 12,000')+' 字符',true);
     if(!isUpdate && body.command.includes(String.fromCharCode(0)))
       return toast('命令不能包含空字符',true);
     if(body.enabled&&!confirm('确认允许此商品的客户自主触发「'+body.label+'」？命令将按已配置身份执行。'))return;
     try{await request('/api/business/admin/self-actions','POST',body);toast('自助操作已保存');reset();await refresh();}
     catch(ex){toast(ex.message,true);}
   };
 });
 return {refresh,products:[]};
})();

;

/* ===== remote-admin.js ===== */
'use strict';
(()=>{
 const by=id=>document.getElementById(id),txt=(tag,s,cls='')=>{const e=document.createElement(tag);e.textContent=String(s??'');if(cls)e.className=cls;return e;};
 const time=s=>window.VMOTime?window.VMOTime(s):s||'—';

 function showCopyableSecret(label,value,reportedAt){
   const overlay=document.createElement('div');overlay.className='vmo-secret-overlay';
   overlay.setAttribute('role','dialog');overlay.setAttribute('aria-modal','true');
   const card=document.createElement('div');card.className='vmo-secret-card';
   const heading=document.createElement('h3');heading.textContent=label+' · 授权信息';
   const hint=document.createElement('p');hint.textContent='保存的是最后一次真实上报的访问码；若软件更换密码，需要重新上报。';
   const reported=document.createElement('p');reported.textContent='访问码上报时间：'+(reportedAt?time(reportedAt):'历史记录（时间未知）');
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

 const states={connected:'WSS 已连接',disconnected:'WSS 已断开',heartbeat_timeout:'心跳超时',never_connected:'尚未连接'};
 let vm='',csrf='',seq=0,page=1,connection=false,opsFingerprint='',appsFingerprint='';
 let selfFingerprint='',selfSubmitting='',installedAgentVersion='', previewOn=false, previewBusy=false, previewTimer=null;
 let previewController=null, previewUrl=null, previewGeneration=0;
 const commandLimit=()=>by('remoteShell').value==='powershell'?12000:8000;
 function versionAtLeast(actual,required){
   const a=String(actual||'').split('.').map(Number), b=required.split('.').map(Number);
   if(a.length<3||a.some(n=>!Number.isInteger(n)))return false;
   for(let i=0;i<3;i++){if(a[i]>b[i])return true;if(a[i]<b[i])return false;}
   return true;
 }
 function updateCommandLimit(){
   const field=by('remoteCommand'),limit=commandLimit(),count=field.value.length;
   field.maxLength=limit;
   const label=by('remoteCommandCount');
   label.textContent=`${count.toLocaleString('zh-CN')} / ${limit.toLocaleString('zh-CN')} 字符（${by('remoteShell').value==='powershell'?'PowerShell':'CMD'}）`;
   label.classList.toggle('remote-count-error',count>limit);
   commandButton();
 }
 function previewVisible(){return !!vm&&by('remoteVm').value===vm&&!!document.querySelector('#page-remote')?.classList.contains('active')&&!document.hidden;}
 function clearPreviewImage(){
   by('remotePreviewImage').hidden=true;by('remotePreviewImage').removeAttribute('src');
   if(previewUrl){URL.revokeObjectURL(previewUrl);previewUrl=null;}
 }
 function stopPreview(message){
   previewOn=false;previewGeneration++;
   if(previewTimer){clearInterval(previewTimer);previewTimer=null;}
   if(previewController){previewController.abort();previewController=null;}
   previewBusy=false;clearPreviewImage();
   by('remotePreviewToggle').textContent='开启预览';by('remotePreviewToggle').setAttribute('aria-pressed','false');
   by('remotePreviewFrame').classList.remove('is-live');
   by('remotePreviewEmpty').hidden=false;
   by('remotePreviewEmpty').innerHTML='预览已关闭<br><small>选择虚拟机并点击「开启预览」</small>';
   by('remotePreviewStatus').textContent=message||'预览已关闭 · 不产生截图请求';
 }
 async function previewTick(){
   if(!previewOn)return;
   if(!previewVisible()){stopPreview('已自动停止：切换虚拟机、离开页面或页面不可见');return;}
   if(previewBusy)return; // do not overlap slow VNC fetches
   const id=vm,generation=previewGeneration;
   const controller=new AbortController();previewController=controller;previewBusy=true;
   if(by('remotePreviewImage').hidden)by('remotePreviewStatus').textContent='正在获取截图…';
   try{
     const response=await fetch(`/api/vms/${encodeURIComponent(id)}/screen-preview.jpg?t=${Date.now()}`,{
       credentials:'same-origin',cache:'no-store',signal:controller.signal
     });
     if(!response.ok){
       let detail='';try{detail=(await response.json()).detail||'';}catch{}
       throw Error(detail||`HTTP ${response.status}`);
     }
     const imageBlob=await response.blob();
     if(!previewOn||generation!==previewGeneration||!previewVisible())return;
     const nextUrl=URL.createObjectURL(imageBlob),previous=previewUrl;
     previewUrl=nextUrl;by('remotePreviewImage').src=nextUrl;
     by('remotePreviewImage').hidden=false;by('remotePreviewEmpty').hidden=true;
     if(previous)URL.revokeObjectURL(previous);
     by('remotePreviewStatus').textContent=`画面更新：${new Date().toLocaleTimeString('zh-CN',{hour12:false})} · 约 1 帧/秒 · ${Math.max(1,Math.round(imageBlob.size/1024))} KB`;
   }catch(e){
     if(e.name==='AbortError'||generation!==previewGeneration)return;
     stopPreview('截图失败：'+e.message+'。如未启用本机 VNC 截图，需先关机并在「虚拟机」页面启用，然后重新开机。');
   }finally{if(previewController===controller){previewController=null;previewBusy=false;}}
 }
 function startPreview(){
   if(!vm)return;
   stopPreview();previewOn=true;
   by('remotePreviewToggle').textContent='关闭预览';by('remotePreviewToggle').setAttribute('aria-pressed','true');
   by('remotePreviewFrame').classList.add('is-live');
   by('remotePreviewEmpty').innerHTML='正在获取首帧…';
   previewTick();previewTimer=setInterval(previewTick,1000);
 }

 async function api(path,method='GET',body){
   if(!csrf){const r=await fetch('/ui/session',{credentials:'same-origin'});if(!r.ok)throw Error('请先登录管理员后台');csrf=(await r.json()).csrf;}
   const r=await fetch(path,{method,credentials:'same-origin',cache:'no-store',headers:{...(method!=='GET'?{'X-UI-CSRF':csrf}:{}),...(body===undefined?{}:{'Content-Type':'application/json'})},body:body===undefined?undefined:JSON.stringify(body)});
   const d=await r.json().catch(()=>({}));if(!r.ok)throw Error(d.detail||'HTTP '+r.status);return d;
 }
 function requireVm(){const id=by('remoteVm').value;if(!id)throw Error('请选择虚拟机');return id;}
 function commandButton(){const text=by('remoteCommand').value;by('remoteExecBtn').disabled=!connection||!text.trim()||text.length>commandLimit()||(text.length>2000&&!versionAtLeast(installedAgentVersion,'2.0.21'));}
 function present(p){
   connection=p.state==='connected';commandButton();
   const target=by('remoteConnection');target.replaceChildren();
   target.append(txt('strong',states[p.state]||p.state));
   target.append(txt('p','最近心跳 · '+time(p.last_wss_heartbeat)));
   target.append(txt('p','最近断开 · '+(p.disconnect_reason||'无')));
   target.append(txt('p','客户端异常 · '+(p.last_client_error||'无')));
   if(p.history?.length){const details=document.createElement('details');
     details.append(txt('summary','连接事件（最近 '+p.history.length+' 条）'));
     for(const row of p.history.slice(0,8))details.append(txt('p',time(row.recorded_at)+' · '+row.event+(row.reason?' · '+row.reason:'')));
     target.append(details);
   }
 }
 function renderAgentUpgrade(info){
   const text=by('remoteAgentUpgradeStatus'),button=by('remoteAgentUpgradeBtn');
   const installed=info.installed_version||'尚未上报';const bundled=info.bundled_version||'未知';
   installedAgentVersion=info.installed_version||'';commandButton();
   const task=info.latest;
   let detail=info.auto_update_enabled?' · 自动更新已开启（空闲时自动升级）':' · 自动更新已关闭';
   if(task){
     if(info.verified)detail=' · 新版本已由 WSS 心跳确认';
     else if(task.status==='FAILED')detail=' · 最近安装任务失败（请检查日志）';
     else if(task.status==='TIMED_OUT')detail=' · 结果未知，请先检查服务后再操作';
     else detail=' · 更新已提交，等待新版本心跳确认（'+(task.status||'准备中')+'）';
   }
   text.textContent='Guest Agent：'+installed+' / 宿主机提供 '+bundled+detail;
   button.disabled=!connection||installed===bundled||Boolean(task&&!info.verified&&task.created_at&&Date.now()-Date.parse(task.created_at)<15*60*1000);
 }
 function renderApps(rows){
   const fingerprint=JSON.stringify(rows.map(a=>[a.app_id,a.version,a.auth_state,a.updated_at,a.reported_at,a.secret_reported_at,a.is_available]));
   if(fingerprint===appsFingerprint)return;appsFingerprint=fingerprint;
   const target=by('remoteApps');target.replaceChildren();
   if(!rows.length){target.append(txt('p','尚未收到应用登记 · 只转发客户机真实上报的入口','muted'));return;}
   for(const app of rows){
     const row=txt('div','','remote-app-row'),info=txt('div','');
     info.append(txt('strong',app.app_id==='snowluma'?'SnowLuma':'DSH-fish-soul'),
       txt('small','授权状态：'+({configured:'已上报访问码（有效性未验证）',unconfigured:'尚未登记访问码',not_reported:'访问码未上报'}[app.auth_state]||app.auth_state)+' · '+(app.is_available?'运行中':'未检测到运行')+' · '+(app.version||'版本未知')),
       txt('small','软件上报：'+time(app.reported_at)),
       txt('small','访问码上报：'+time(app.secret_reported_at)),
       txt('small','状态心跳：'+time(app.updated_at)));
     const b=txt('button','查看已上报访问码','btn ghost small');
     b.onclick=async()=>{if(!confirm('查看 '+app.app_id+' 的敏感访问码？'))return;
       try{const r=await api('/api/vms/'+encodeURIComponent(vm)+'/remote/apps/'+encodeURIComponent(app.app_id)+'/reveal','POST',{confirm:true});showCopyableSecret(app.app_id==='snowluma'?'SnowLuma':'DSH-fish-soul',r.access_code,r.reported_at);}catch(e){alert(e.message);}};
     row.append(info,b);target.append(row);
   }
 }
 function generateRequestId(){
   const bytes=new Uint8Array(16);
   if(!window.crypto?.getRandomValues)throw Error('需要受支持的浏览器随机数接口以安全提交自助操作');
   window.crypto.getRandomValues(bytes);
   bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
   const hex=[...bytes].map(x=>x.toString(16).padStart(2,'0')).join('');
   return hex.slice(0,8)+'-'+hex.slice(8,12)+'-'+hex.slice(12,16)+'-'+hex.slice(16,20)+'-'+hex.slice(20);
 }
 function renderSelfActions(info){
   const rows=info?.items||[];
   const key=JSON.stringify([vm,connection,selfSubmitting,info?.message,
      rows.map(a=>[a.id,a.label,a.description,a.kind,a.updated_at])]);
   if(key===selfFingerprint)return;
   selfFingerprint=key;
   const status=by('remoteSelfActionsStatus'),target=by('remoteSelfActions');
   const before=target.querySelector('select')?.value||'';
   status.textContent=info?.message||'当前虚拟机没有已授权的自助操作';
   target.replaceChildren();
   if(!rows.length)return;
   const bar=txt('div','','remote-self-controls');
   const select=document.createElement('select');
   select.setAttribute('aria-label','选择客户自助操作');
   select.append(new Option('请选择自助操作',''));
   rows.forEach(item=>select.add(new Option(item.label+
      (item.kind==='SOFTWARE_UPDATE'?' · 软件更新':''),item.id)));
   select.value=rows.some(x=>x.id===before)?before:rows[0].id;
   const button=txt('button','执行操作','btn primary small');
   button.type='button';
   const help=txt('p','','muted remote-self-description');
   function selection(){
     const selected=rows.find(x=>x.id===select.value);
     help.textContent=selected?.description||
       (selected?'管理员已授权的快捷操作':'请选择操作');
     button.disabled=!connection||!selected||Boolean(selfSubmitting);
   }
   select.onchange=selection;
   button.onclick=async()=>{
     const selected=rows.find(x=>x.id===select.value);
     if(!connection)return alert('Guest WSS 未连接，无法执行');
     if(!selected||selfSubmitting)return;
     if(!confirm('确认对当前客户虚拟机执行「'+selected.label+'」？\n\n'+
       (selected.description||'执行后无法撤销，请勿重复提交。')+
       '\n执行结果将在「最近执行记录」显示。'))return;
     const requestVm=vm;
     selfSubmitting=selected.id;selfFingerprint='';button.disabled=true;
     status.textContent='正在下发「'+selected.label+'」…';
     try{
       const result=await api('/api/vms/'+encodeURIComponent(requestVm)+
         '/remote/self-actions/'+encodeURIComponent(selected.id)+'/execute','POST',
         {request_id:generateRequestId(),confirm:true});
       status.textContent='已提交 '+result.request_id+'，请查看最近执行记录';
     }catch(err){status.textContent='操作未下发：'+err.message;alert(status.textContent);}
     finally{selfSubmitting='';selfFingerprint='';await refresh();}
   };
   bar.append(select,button);
   target.append(bar,help);
   selection();
 }
 async function loadRecentOps(vmId,requestedPage){
   const needed=Math.max(5,requestedPage*5);
   let count=0,total=0;const items=[];
   while(count<needed){
     const size=Math.min(100,needed-count);
     const batch=await api('/api/vms/'+encodeURIComponent(vmId)+
       '/remote/operations?paged=true&page='+Math.floor(count/size+1)+
       '&page_size='+size);
     total=batch.total;
     items.push(...(batch.items||[]));
     count+=size;
     if(items.length>=total||!(batch.items||[]).length)break;
   }
   return {items,total};
 }
 function renderOps(paged, self){
   // The controller holds the page; loadRecentOps fetches sufficient rows.
   const history=self?.executions||[];
   const commands=new Map(history.filter(x=>x.action_kind==='COMMAND'&&x.operation_id).map(x=>[x.operation_id,x]));
   const updates=history.filter(x=>x.action_kind==='SOFTWARE_UPDATE');
   const total=(paged.total||0)+updates.length;
   const pages=Math.max(1,Math.ceil(total/5));
   page=Math.max(1,Math.min(page,pages));
   by('remotePageLabel').textContent='第 '+page+' / '+pages+' 页 · '+total+' 条（每页5条）';
   by('remotePrev').disabled=page<=1;by('remoteNext').disabled=page>=pages;
   const ops=paged.items||[];
   const fingerprint=JSON.stringify([page,ops.map(o=>[o.id,o.state,o.attempt_count,o.completed_at,o.last_error,o.result]),
     updates.map(x=>[x.request_id,x.status,x.updated_at,x.output_text]),history.map(x=>[x.operation_id,x.label])]);
   if(fingerprint===opsFingerprint)return;opsFingerprint=fingerprint;
   const target=by('remoteOps');const opened=new Set([...target.querySelectorAll('details[open]')].map(x=>x.dataset.id));
   target.replaceChildren();
   const merged=[...ops.map(x=>({type:'op',time:x.created_at,entry:x})),
     ...updates.map(x=>({type:'self-update',time:x.created_at,entry:x}))];
   merged.sort((a,b)=>Date.parse(b.time||'')-Date.parse(a.time||''));
   const visible=merged.slice((page-1)*5,page*5);
   if(!visible.length){target.append(txt('p','当前页暂无执行记录','muted'));return;}
   for(const item of visible){
     const row=txt('div','','remote-op-card'),head=txt('div','','remote-op-header');
     if(item.type==='self-update'){
       const task=item.entry;
       head.append(txt('strong','客户自助 · '+task.label+' · '+task.status),
                   txt('span',time(task.created_at)));
       row.append(head);
       if(task.status==='TIMED_OUT'||task.status==='UNKNOWN')
         row.append(txt('span','结果未知：请先核对 Guest 日志，不会自动重复执行。','muted'));
       if(task.output_text){
         const d=document.createElement('details');
         d.dataset.id=task.request_id;d.open=opened.has(task.request_id);
         d.append(txt('summary','查看更新结果 · '+task.request_id),
                  txt('pre',task.output_text.slice(0,1200)));row.append(d);
       }else row.append(txt('small','更新状态及步骤由客户端回报；暂无额外输出','muted'));
       target.append(row);continue;
     }
     const op=item.entry, selfAction=commands.get(op.id);
     const actionLabel=selfAction?'客户自助 · '+selfAction.label:op.action;
     const label=txt('strong',actionLabel+' · '+op.state);const meta=txt('span',time(op.created_at));
     head.append(label,meta);row.append(head);
     if(op.state==='CLAIMED')row.append(txt('span','已投递，等待开始确认','muted'));
     if(op.state==='RUNNING')row.append(txt('span','客户端正在执行','muted'));
     if(op.state==='TIMED_OUT')row.append(txt('span','执行结果未知：避免自动重复执行；请先查看进程和日志。','muted'));
     if(op.last_error && !(op.action==='COMMAND'&&op.state==='FAILED'))row.append(txt('span',op.last_error,'muted'));
     if(op.attempt_count>1)row.append(txt('span','投递尝试次数（非执行次数）：'+op.attempt_count,'muted'));
     const result=op.result||{};
     if(result.error==='TimeoutExpired')row.append(txt('span','命令超时：先检查进程和日志，不自动重复执行。','muted'));
     if(op.action==='COMMAND'&&op.state==='FAILED'){
       const message=String(result.stderr||result.error||op.last_error||'命令执行失败，没有错误输出')
         .split(/\r?\n/).map(x=>x.trim()).filter(Boolean).slice(0,6).join('\n').slice(0,900);
       row.append(txt('pre',message,'vmo-command-error'));
     }else if(op.action==='COMMAND'){
       const output=[result.stdout?String(result.stdout).trim():'',result.stderr?String(result.stderr).trim():''].filter(Boolean).join('\n');
       if(output){
         const d=document.createElement('details');d.dataset.id=op.id;d.open=opened.has(op.id);
         d.append(txt('summary','查看输出 · '+op.id),txt('pre',output));row.append(d);
       }else row.append(txt('small',op.id,'muted'));
     }else if(Object.keys(result).length){
       const d=document.createElement('details');d.dataset.id=op.id;d.open=opened.has(op.id);
       d.append(txt('summary','查看输出 · '+op.id),txt('pre',JSON.stringify(result,null,2)));row.append(d);
     }else row.append(txt('small',op.id,'muted'));
     target.append(row);
   }
 }
 async function refresh(){
   const at=++seq;
   try{
     const [vms,guests]=await Promise.all([api('/api/vms'),api('/api/fleet/guests')]);if(at!==seq)return;
     const choice=by('remoteVm'),previous=choice.value||vm;
     const ready=vms.filter(v=>v.state==='READY');const signature=ready.map(v=>v.id+':'+v.name).join('|');
     if(choice.dataset.options!==signature){choice.dataset.options=signature;choice.replaceChildren(new Option('请选择客户虚拟机',''));for(const v of ready)choice.add(new Option(v.name,v.id));}
     choice.value=ready.some(v=>v.id===previous)?previous:'';
     if(choice.value!==vm){stopPreview('虚拟机已切换，预览需要重新手动开启');vm=choice.value;page=1;appsFingerprint='';opsFingerprint='';selfFingerprint='';}
     by('remotePreviewToggle').disabled=!vm;
     if(!vm){installedAgentVersion='';connection=false;commandButton();by('remoteConnection').textContent='选择虚拟机后展示 WSS 状态';by('remotePermissionStatus').textContent='等待选择虚拟机';by('remoteAgentUpgradeBtn').disabled=true;by('remoteAgentUpgradeStatus').textContent='先选择虚拟机';by('remoteSelfActions').replaceChildren();by('remoteSelfActionsStatus').textContent='请选择虚拟机';return;}
     const g=guests.find(x=>x.vm_id===vm&&!x.revoked_at);
     by('remotePermissionStatus').textContent=g?'已纳管 · 宿主机管理员可下发 SYSTEM 命令':'未完成 Agent 身份注册';
     const requestVm=vm;
     const [presence,ops,apps,upgrade,self]=await Promise.all([
        api('/api/vms/'+encodeURIComponent(vm)+'/remote/presence'),
        loadRecentOps(vm,page),
        api('/api/vms/'+encodeURIComponent(vm)+'/remote/apps'),
        api('/api/vms/'+encodeURIComponent(vm)+'/agent-upgrade'),
         api('/api/vms/'+encodeURIComponent(vm)+'/remote/self-actions').catch(err=>({items:[],executions:[],message:'自助操作读取失败：'+err.message}))]);
     if(at!==seq||requestVm!==by('remoteVm').value)return;
     present(presence);renderOps(ops,self);renderApps(apps);renderAgentUpgrade(upgrade);renderSelfActions(self);
   }catch(e){by('remoteConnection').textContent='状态获取失败：'+e.message;connection=false;commandButton();}
 }
 async function submit(kind,data){
   try{const id=requireVm();const x=await api('/api/vms/'+encodeURIComponent(id)+'/remote/'+kind,'POST',data);
     by('remotePermissionStatus').textContent='已提交 '+x.operation_id+' · 结果将在下方更新';
     await refresh();
   }catch(e){alert('命令未下发：'+e.message);}
 }
 window.addEventListener('DOMContentLoaded',()=>{
   by('remoteRefresh').onclick=refresh;
   by('remoteAgentUpgradeBtn').onclick=async()=>{
     try{
       if(!connection)throw Error('Guest WSS 未连接');
       const id=requireVm();
       const state=await api('/api/vms/'+encodeURIComponent(id)+'/agent-upgrade');
       if(!confirm('将从宿主机把 '+id+' 的 Guest Agent 升级到 '+state.bundled_version+'。\n升级会短暂中断 WSS；现有 Agent 身份和授权码不应改变。请先备份客户 VM。\n确认启动一次更新？'))return;
       by('remoteAgentUpgradeBtn').disabled=true;
       const task=await api('/api/vms/'+encodeURIComponent(id)+'/agent-upgrade','POST',{confirm:true});
       by('remoteAgentUpgradeStatus').textContent='升级任务 '+task.operation_id+' 已提交，等待新版本心跳确认';
       await refresh();
     }catch(e){alert('Guest Agent 更新未下发：'+e.message);await refresh();}
   };
   by('remoteVm').onchange=()=>{stopPreview('虚拟机已切换，预览需要重新手动开启');vm=by('remoteVm').value;page=1;opsFingerprint='';appsFingerprint='';refresh();};
   by('remotePreviewToggle').onclick=()=>{if(previewOn)stopPreview();else startPreview();};
   document.addEventListener('visibilitychange',()=>{if(document.hidden&&previewOn)stopPreview('页面不可见，已自动停止预览');});
   window.addEventListener('beforeunload',()=>{if(previewOn)stopPreview();});
   by('remoteShell').onchange=updateCommandLimit;updateCommandLimit();
   by('remotePrev').onclick=()=>{if(page>1){page--;opsFingerprint='';refresh();}};
   by('remoteNext').onclick=()=>{page++;opsFingerprint='';refresh();};
   by('remoteCommand').addEventListener('input',()=>{
     const isLauncher=/^start\s+(?:""\s+)?/i.test(by('remoteCommand').value.trim());
     if(isLauncher&&by('remoteRunMode').value==='wait')by('remoteRunMode').value='interactive';
     updateCommandLimit();
   });
   by('remoteDiagBtn').onclick=()=>submit('diagnostic',{name:by('remoteDiagnostic').value});
   by('remoteExecBtn').onclick=()=>{
     if(!connection)return alert('WSS 当前未连接');
     const cmd=by('remoteCommand').value.trim();if(!cmd)return alert('请填写命令');
     if(cmd.length>commandLimit())return alert('命令过长：'+by('remoteShell').value+' 最多 '+commandLimit()+' 字符');
     if(cmd.length>2000&&!versionAtLeast(installedAgentVersion,'2.0.21'))return alert('命令超过旧 Agent 的 2,000 字符限制，请先点击上方「升级 Guest Agent」并确认版本为 v2.0.21 或更新。');
     submit('command',{shell:by('remoteShell').value,command:cmd,
       privilege:'system',run_mode:by('remoteRunMode').value,cwd:by('remoteCwd').value.trim(),
       timeout_seconds:Number(by('remoteTimeout').value),confirm:true});
   };
   by('remoteListBtn').onclick=()=>submit('files/list',{area:by('remoteFileArea').value});
   by('remoteLogBtn').onclick=()=>submit('files/read',{filename:by('remoteLogName').value});
   by('remoteUploadBtn').onclick=async()=>{
     const status=by('remoteUploadStatus'),bar=by('remoteUploadProgress'),button=by('remoteUploadBtn');
     try{const id=requireVm(),file=by('remoteFile').files[0],directory=by('remoteUploadDirectory').value.trim();
       if(!file)throw Error('请选择文件');
       if(!file.name||file.name.length>180||/[<>:"/\\|?*\x00-\x1f]/.test(file.name)||/[. ]$/.test(file.name))throw Error('文件名不符合 Windows 文件名规则');
       if(!/^[a-zA-Z]:\\/.test(directory))throw Error('目标目录必须是 Windows 本地绝对路径，例如 C:\\dsh\\');
       if(!csrf){const r=await fetch('/ui/session',{credentials:'same-origin'});csrf=(await r.json()).csrf;}
       button.disabled=true;bar.style.display='block';bar.value=0;status.textContent='正在向宿主机上传…';
       const result=await new Promise((resolve,reject)=>{
         const xhr=new XMLHttpRequest();
         xhr.open('POST','/api/vms/'+encodeURIComponent(id)+'/remote/upload/'+encodeURIComponent(file.name),true);
         xhr.withCredentials=true;
         xhr.setRequestHeader('X-UI-CSRF',csrf);
         xhr.setRequestHeader('X-VMO-Upload-Directory',directory);
         xhr.setRequestHeader('Content-Type','application/octet-stream');
         xhr.upload.onprogress=e=>{if(e.lengthComputable){bar.value=Math.round(100*e.loaded/e.total);
           status.textContent='上传到宿主机：'+bar.value+'%（随后等待 Agent 拉取）';}};
         xhr.onerror=()=>reject(Error('上传网络中断：检查 8765 连接及反向代理请求体限制'));
         xhr.onload=()=>{let x={};try{x=JSON.parse(xhr.responseText);}catch{}
           if(xhr.status>=200&&xhr.status<300)resolve(x);
           else reject(Error(x.detail||('HTTP '+xhr.status+'：请检查反向代理的上传大小限制')));};
         xhr.send(file);
       });
       status.textContent='宿主机接收成功（'+result.bytes+' 字节），任务 '+result.operation_id+' 正在等待 Guest 下载到 '+result.directory;
       await refresh();
     }catch(e){status.textContent='上传失败：'+e.message;alert(e.message);}
     finally{button.disabled=false;}
   };
   window.addEventListener('vmo:changed',()=>{if(document.querySelector('#page-remote')?.classList.contains('active')&&!document.hidden)refresh();});
   setInterval(()=>{if(document.querySelector('#page-remote')?.classList.contains('active')&&!document.hidden)refresh();},7000);
 });
 window.VMORemote={refresh,stopPreview};
})();

;


/* ===== System update — host self-update, NOT Guest software updates ===== */
(() => {
 let latest='', available=false, busy=false, timer=null, offlinePolls=0;
 const setText=(id,value)=>{const node=document.getElementById(id);if(node)node.textContent=String(value??'—');};
 function buttonState(){const install=document.getElementById('sysInstallBtn');if(install)install.disabled=busy;const check=document.getElementById('sysCheckBtn');if(check)check.disabled=busy;}
 async function load(){
   try{const data=await request('/api/system/update');show(data);}
   catch(e){setText('sysProgress','无法读取更新状态：'+e.message);}
 }
 function isNewer(a,b){
   const nums=v=>/^\d+\.\d+\.\d+$/.test(v)?v.split('.').map(Number):null;
   const x=nums(String(a||'').replace(/^v/,'')),y=nums(String(b||'').replace(/^v/,''));
   if(!x||!y)return false;
   for(let i=0;i<3;i++){if(x[i]!==y[i])return x[i]>y[i];}
   return false;
 }
 function show(data){
   const running=String(data.current||'');
   const disk=String(data.disk_version||'');
   const target=String(data.update?.version||'');
   const update=data.update||{};
   const confirmed=(update.state==='success' && target===running &&
      (!disk||disk===running) && (update.mode!=='restart'||
       !data.started_version||data.started_version===running));
   const mismatch=update.state==='version_mismatch'||
      (update.state==='success'&&!confirmed);
   const newest=String(data.latest||latest||'').replace(/^v/,'');
   setText('sysCurrentVersion','v'+running);setText('sysRepo',data.repository);
   if(mismatch){
      available=false;
      setText('sysAvailability','运行版本不一致');
      setText('sysUpdateBadge','需要核查');
   }else if(newest){
      available=isNewer(newest,running);
      setText('sysAvailability',available?'有新版本':'已是最新正式版');
      setText('sysUpdateBadge',available?'可更新':confirmed?'更新成功':'已是最新');
   }else if(confirmed){
      available=false;
      setText('sysAvailability','已安装');
      setText('sysUpdateBadge','更新成功');
   }
   // A version-label mismatch must not cause an infinite refresh loop.
   // Users can refresh manually after a confirmed update; ?page=system persists.
   const loadedScript=document.querySelector('script[src*="/ui/admin.js"]');
   const loadedVer=loadedScript?.getAttribute('src')?.match(/[?&]v=([0-9.]+)/)?.[1];
   const staleBrowser=confirmed&&loadedVer&&running!==loadedVer;
   setText('sysAuth',data.configured_token?'已配置私有仓库令牌':'无令牌：只可访问公开的 Releases');
   const names={idle:'未开始',downloading:'下载中',verified:'校验通过',handoff:'启动独立更新器',preparing:'独立更新器已启动',stopping:'停止旧版服务',backing_up:'备份旧版程序',applying:'正在替换程序',restarting:'重新启动',hot_applying:'页面热更新中',unconfirmed:'等待人工确认',version_mismatch:'版本不一致',success:'已完成',failed:'更新失败',rolled_back:'已回滚'};
   setText('sysProgress',mismatch?
      '更新记录与实际版本不符：运行 v'+running+' / 磁盘 v'+(disk||'未知')+
      ' / 目标 v'+(target||'未知')+'。请核对安装路径，暂勿重复升级。':
      (names[update.state]||update.state||'未开始')+' · '+
      (update.mode==='hot'?'[免重启] ':update.mode==='restart'?'[安全重启] ':'')+
      (update.message||'等待操作'));
   if(staleBrowser)setText('sysProgress',
     (document.getElementById('sysProgress')?.textContent||'')+
     '。当前页面资源来自旧版本，请按 Ctrl+F5 刷新（会保留本页面）。');
   if(['downloading','verified','handoff','preparing','stopping','backing_up','applying','restarting','hot_applying'].includes(update.state)){
     busy=true;buttonState(); if(!timer)timer=setInterval(poll,2500);
   } else {
     busy=false;buttonState();if(timer){clearInterval(timer);timer=null;}
   }
 }
 async function poll(){if(state.activePage!=='system'){clearInterval(timer);timer=null;return;}
  try{show(await request('/api/system/update'));offlinePolls=0;}catch(e){offlinePolls++;setText('sysProgress',offlinePolls>12?'管理端已离线超过30秒。若未自动恢复，请查看 data/updates/updater.log 和 updater-bootstrap.log，并运行修复工具。':'服务暂时离线，正在等待更新器重启管理端…');}
 }
 async function check(){
  if(busy)return;setText('sysUpdateBadge','检查中');setText('sysProgress','正在连接 GitHub Releases…');
  try{
   const info=await request('/api/system/update?check=true');latest=info.latest||'';available=Boolean(info.available);
   setText('sysLatestVersion','v'+latest);setText('sysAvailability',available?'有新版本':'已是最新正式版');
   setText('sysUpdateBadge',available?'可更新':'已是最新');
   const node=document.getElementById('sysReleaseNotes');node.textContent=info.notes||'此 Release 未填写说明';node.hidden=false;
   show(info);buttonState();
  }catch(e){available=false;setText('sysUpdateBadge','检查失败');setText('sysProgress',e.message);buttonState();}
 }
 async function install(){
  if(busy)return;
  // One-click path checks GitHub before showing the installation confirmation.
  await check();
  if(!available||!latest)return;
  if(!confirm('确认更新到 v'+latest+'？\n\n系统会先检查变更：仅 HTML/CSS/JS 时免重启热替换；后端/依赖变更时先备份并安全重启。\n已有数据库、密钥、Guest Agent 身份和虚拟机不会覆盖。建议先确认未在执行 VM 操作。'))return;
  try{
   const result=await request('/api/system/update/apply','POST',{version:latest,confirm:true});
   available=false;busy=true;buttonState();setText('sysProgress','更新已提交：v'+result.version+'，下载与校验开始');
   if(!timer)timer=setInterval(poll,2500);
  }catch(e){setText('sysProgress','更新未启动：'+e.message);}
 }
 window.VMOSystemUpdate={load,check};
 window.addEventListener('DOMContentLoaded',()=>{
  document.getElementById('sysLogBtn')?.addEventListener('click',async()=>{
    const pre=document.getElementById('sysUpdateLogs');
    if(!pre)return;
    pre.hidden=false;pre.textContent='正在获取更新器日志…';
    try{
      const data=await request('/api/system/update/log');
      pre.textContent='本次更新标识：'+(data.attempt||'旧版未提供')+'\n\n'+Object.entries(data.logs||{}).map(([name,content])=>
        '--- '+name+' ---\n'+content).join('\n\n');
    }catch(e){pre.textContent='无法读取日志：'+e.message;}
  });
  document.getElementById('sysCheckBtn')?.addEventListener('click',check);
  document.getElementById('sysInstallBtn')?.addEventListener('click',install);
 });
})();



/* ===== Verified Host restart and background log inspection (no Stop UI) ===== */
(() => {
  let restarting=false, originalPid=null, attemptId='', pending=null, checks=0;
  const by=id=>document.getElementById(id);
  const updateText=value=>{if(by('hostProcessStatus'))by('hostProcessStatus').textContent=value;};
  function badge(message,error=false){
    const node=by('hostProcessBadge');
    if(node){node.textContent=message;node.className='pill '+(error?'bad':'ok');}
  }
  function show(info){
    const op=info.restart||{};
    if(!restarting)updateText('VMO 服务运行中 · PID '+(info.pid||'—')+
      ' · 启动约 '+Math.floor((info.uptime_seconds||0)/60)+' 分钟'+
      (op.message?' · 最近重启：'+op.message:''));
    badge(info.running?'运行中':'未知',!info.running);
  }
  async function load(){
    try{show(await request('/api/system/host/status'));}
    catch(e){badge('读取失败',true);updateText('无法获取后台服务状态：'+e.message);}
  }
  async function logs(){
    const panel=by('hostRuntimeLogs');
    if(!panel)return;
    panel.hidden=false;panel.textContent='正在读取后台日志…';
    try{
      const data=await request('/api/system/host/logs');
      const labels={update_stdout:'升级后后台启动输出',update_stderr:'升级后后台启动错误',
        restart_audit:'重启操作记录',restart_stdout:'最近重启标准输出',
        restart_stderr:'最近重启标准错误',restart_bootstrap:'重启控制器错误'};
      panel.textContent=Object.entries(data.logs||{}).map(([key,value])=>
        '--- '+(labels[key]||key)+' ---\n'+(String(value||'').trim()||'无输出')).join('\n\n');
    }catch(e){panel.textContent='后台日志获取失败：'+e.message;}
  }
  function finish(message,error=false){
    restarting=false;
    if(pending){clearInterval(pending);pending=null;}
    if(by('hostRestartBtn'))by('hostRestartBtn').disabled=false;
    if(message)updateText(message);
    if(error)badge('重启异常',true);
  }
  async function poll(){
    if(!restarting)return;
    checks++;
    try{
      const info=await request('/api/system/host/status');
      const op=info.restart||{};
      if(op.attempt===attemptId&&op.state==='failed'){
        finish('重启失败：'+(op.message||'请查看后台日志'),true);return;
      }
      if(op.attempt===attemptId&&op.state==='running'&&info.pid!==originalPid){
        badge('重启成功');
        finish('管理程序已重启，当前 PID '+info.pid+'；客户虚拟机仍保持原状态');
        return;
      }
      updateText('管理程序正在重启：'+(op.message||'等待服务恢复…'));
    }catch(e){updateText('管理端暂时断线，正在尝试重新连接…');}
    if(checks>=50)finish('尚未确认重启结果。请刷新状态并查看后台日志，暂勿重复重启。',true);
  }
  async function restart(){
    if(restarting)return;
    if(!confirm('确定重启 VM Orchestrator 管理程序？\n\n网页和 Guest 网关会短暂断线；不关闭任何 VMware 虚拟机。运行中的管理任务可能被中断。'))return;
    restarting=true;checks=0;
    if(by('hostRestartBtn'))by('hostRestartBtn').disabled=true;
    updateText('正在提交重启请求…');
    try{
      const original=await request('/api/system/host/status');
      originalPid=original.pid;
      const result=await request('/api/system/host/restart','POST',{confirm_restart:true});
      attemptId=result.attempt;
      updateText('重启请求已接收，正在等待管理程序重新上线…');
      pending=setInterval(poll,3000);
    }catch(e){finish('未能启动重启：'+e.message,true);}
  }
  window.VMOHostManage={load,logs};
  window.addEventListener('DOMContentLoaded',()=>{
    by('hostStatusRefreshBtn')?.addEventListener('click',load);
    by('hostLogsBtn')?.addEventListener('click',logs);
    by('hostRestartBtn')?.addEventListener('click',restart);
  });
})();

/* ===== cleanup center; never infer deletability from a directory name ===== */
(function(){
 'use strict';
 let candidates=[],missingRecords=[],scanning=false,deleting=false;
 const fmt=n=>{const x=Number(n)||0;return x>=1073741824?(x/1073741824).toFixed(2)+' GB':
          x>=1048576?(x/1048576).toFixed(1)+' MB':x>=1024?(x/1024).toFixed(1)+' KB':x+' B';};
 const selected=()=>Array.from(document.querySelectorAll('.vmo-clean-select:checked')).map(c=>c.dataset.id);
 function render(data){
   candidates=data.items||[];
   missingRecords=data.missing_records||[];
   const missing=$('cleanupMissingRecords');
   if(missing){
     missing.replaceChildren();
     for(const item of missingRecords){
       const row=el('div','template-card'),label=el('label','check-row');
       const input=document.createElement('input');input.type='checkbox';
       input.className='vmo-missing-select';input.dataset.job=item.job_id;
       label.append(input,el('strong','','历史备份缺失 · '+item.job_id));
       row.append(label,el('small','',item.path),el('small','',item.reason));
       missing.append(row);
     }
     if(!missing.childElementCount)
       missing.append(el('p','muted','没有待核实的缺失备份记录。'));
   }
   const out=$('cleanupProgramItems'),oldP=$('cleanupOldPackages'),vms=$('cleanupVMBackups'),blocked=$('cleanupProtected');
   if(!out||!oldP||!vms||!blocked)return;
   out.replaceChildren();oldP.replaceChildren();vms.replaceChildren();blocked.replaceChildren();
   const label={program_cache:'旧更新临时缓存',old_log:'旧更新日志',
                code_backup:'历史代码备份',reinstall_backup:'重装 VM 备份',update_artifact:'更新脚本与日志',program_extra:'版本外文件',data_cache:'数据临时文件',old_software_package:'历史软件发布 ZIP'};
   function section(item,root){
     const row=el('div','template-card'),details=el('div');
     const head=el('label','check-row');
     const ck=el('input','vmo-clean-select');ck.type='checkbox';ck.dataset.id=item.id;
     // Old VM backups and code rollback archives are NEVER selected by default.
     ck.checked=!item.manual;
     const name=el('strong','',item.title);head.append(ck,name);
     details.append(head,el('small','',label[item.kind]+' · '+fmt(item.bytes)+' · '+item.files+' 个文件'));
     details.append(el('small','',item.path));
      if(item.reason)details.append(el('small','',item.reason));
     row.append(details);root.append(row);
   }
   candidates.forEach(item=>section(item,item.kind==='reinstall_backup'?vms:item.kind==='old_software_package'?oldP:out));
    if(!oldP.childElementCount)oldP.append(el('p','muted','没有可清理的旧版软件 ZIP；当前版本和被引用的包继续受保护。'));
   if(!out.childElementCount)out.append(el('p','muted','没有符合清理条件的程序文件。'));
   if(!vms.childElementCount)vms.append(el('p','muted','没有通过安全检查的已完成重装备份。'));
   for(const p of (data.protected||[])){
     const row=el('div','template-card');row.append(el('strong','',p.reason||'受保护'),el('small','',p.path||''));blocked.append(row);
   }
   if(!blocked.childElementCount)blocked.append(el('p','muted','没有需要人工处理的候选项目。'));
   $('cleanupSummary').textContent='可检查 '+candidates.length+' 项，合计 '+fmt(data.total_bytes)+
       '；其中旧虚拟机备份 '+(data.reinstall_backups||0)+' 项，缺失记录 '+missingRecords.length+
       ' 条，历史软件 ZIP '+(data.old_software_packages||0)+' 项，受保护 '+(data.protected_count||0)+' 项。';
 }
 async function scan(){
   if(scanning||deleting)return;
   scanning=true;$('cleanupSummary').textContent='正在核验文件目录及重装记录…';
   try{render(await request('/api/system/cleanup/scan'));}
   catch(e){$('cleanupSummary').textContent='扫描未通过：'+e.message;toast(e.message,true);}
   finally{scanning=false;}
 }
 async function clean(){
   if(scanning||deleting)return;
   const keys=selected();
   if(!keys.length)return toast('请先勾选需要清理的项目',true);
   const choices=keys.map(k=>candidates.find(x=>x.id===k)).filter(Boolean);
   const backups=choices.filter(x=>x.kind==='reinstall_backup');
   const packages=choices.filter(x=>x.kind==='old_software_package');
   const bytes=choices.reduce((x,y)=>x+y.bytes,0);
   if(!confirm('即将永久清理 '+choices.length+' 项，约 '+fmt(bytes)+
              '。已删除的数据不能恢复。是否继续？'))return;
   if(packages.length&&!confirm('你选择了 '+packages.length+
     ' 个历史软件 ZIP。已核实当前未生效、未被任务或自助操作引用。\n删除后不能再次部署旧版，执行前将复核数据库及 SHA-256，重装/更新历史保留。确定永久删除？'))return;
   if(backups.length&&!confirm('你单独标记了 '+backups.length+
              ' 份旧虚拟机完整备份；它们可能包含旧系统、快照和客户文件。确认永久删除？'))return;
   if(prompt('请输入“永久清理”确认此不可撤销的操作：','')!=='永久清理')return;
   deleting=true;
   try{
     const result=await request('/api/system/cleanup/delete','POST',{
       confirm_phrase:'永久清理',confirm_backups:backups.length>0,
       items:choices.map(x=>({id:x.id,fingerprint:x.fingerprint,confirm_software:x.kind==='old_software_package'&&packages.length>0}))
     });
     toast('清理完成：'+(result.deleted||[]).length+' 项，约 '+fmt(result.deleted_bytes));
     await scanAfterDelete();
     if(state.csrf)refresh();
   }catch(e){toast('清理中断：'+e.message,true);await scanAfterDelete();}
   finally{deleting=false;}
 }
 async function scanAfterDelete(){
   try{render(await request('/api/system/cleanup/scan'));}
   catch(e){$('cleanupSummary').textContent='请手动检查：'+e.message;}
 }
 window.VMOCleanup={scan};
 window.addEventListener('DOMContentLoaded',()=>{
   $('cleanupScanBtn')?.addEventListener('click',scan);
   $('cleanupSelectSafeBtn')?.addEventListener('click',()=>{
     document.querySelectorAll('.vmo-clean-select').forEach(c=>{
       const item=candidates.find(x=>x.id===c.dataset.id);
       c.checked=Boolean(item&&!item.manual);
     });
   });
   $('cleanupDeleteBtn')?.addEventListener('click',clean);
   $('cleanupAcknowledgeMissing')?.addEventListener('click',async()=>{
     if(scanning||deleting)return;
     const ids=[...document.querySelectorAll('.vmo-missing-select:checked')]
       .map(x=>x.dataset.job).filter(Boolean);
     if(!ids.length)return toast('请勾选需要确认的缺失备份记录',true);
     if(!confirm('将 '+ids.length+' 条已经不存在的历史备份标记为“已核实缺失”。\n不会删除任何虚拟机文件，不会清除重装历史，也不会释放磁盘空间。是否继续？'))return;
     if(prompt('输入“确认备份缺失”以提交数据库状态：','')!=='确认备份缺失')return;
     deleting=true;
     try{
       const result=await request('/api/system/cleanup/acknowledge-missing','POST',{
         job_ids:ids,confirm_phrase:'确认备份缺失'});
       toast('已核实 '+result.acknowledged.length+' 条失效备份记录，未删除任何文件');
       await scanAfterDelete();
     }catch(e){toast('失效记录同步失败：'+e.message,true);await scanAfterDelete();}
     finally{deleting=false;}
   });
 });
})();
