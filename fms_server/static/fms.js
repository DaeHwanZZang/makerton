'use strict';
// 화면 상태는 현장/맵 선택을 기준으로 묶는다. 서버 데이터는 textContent 로 표시한다.
const $ = s => document.querySelector(s);
const enc = encodeURIComponent;
const app = {
  page:'map', sites:[], site:null, detail:null, map:null, locations:[], robots:new Map(), jobs:[], steps:[], edit:null, selection:null, mapToken:0, siteToken:0
};
const titles = {
  map:'실시간 맵', robots:'로봇', jobs:'작업 게시판', presets:'작업 프리셋', maps:'맵 관리', locations:'목적지·도킹', elevators:'엘리베이터', settings:'현장 설정'
};
const types = {
  station:'작업 지점', charger:'충전소', parking:'주차 지점', waypoint:'경유점', other:'기타'
};
const states = {
  IDLE:'대기', BUSY:'작업 중', MOVING:'이동 중', ERROR:'오류', EMERGENCY:'비상 정지', CHARGING:'충전 중', PAUSED:'일시 정지', ONLINE:'연결됨', OFFLINE:'연결 끊김', STALE:'수신 지연', LOST:'연결 유실', UNREGISTERED:'미등록', pending:'배정 대기', assigned:'배정됨 · 응답 대기', canceling:'취소 확인 중', working:'진행 중', success:'완료', failed:'실패', canceled:'취소', LOCALIZED:'위치 확인됨', UNLOCALIZED:'위치 미확정', LOCALIZING:'위치 확인 중'
};
const label = x => states[x] || x || '—';
const siteURL = () => `/api/v1/sites/${enc(app.site)}`;
const mapURL = () => `${siteURL()}/maps/${enc(app.map)}`;
function el(tag, text, cls) {
  const n=document.createElement(tag);
  if(text!==undefined)n.textContent=text;
  if(cls)n.className=cls;
  return n;
}
function button(text, action, cls='') {
  const b=el('button',text,cls);
  b.type='button';
  b.addEventListener('click',()=>run(action));
  return b;
}
function notify(message, bad=false) {
  const box=$('#notice');
  box.hidden=false;
  box.className=bad?'bad':'';
  box.querySelector('span').textContent=message;
  $('#log').prepend(el('div',`${new Date().toLocaleTimeString()} ${message}`,bad?'bad':''));
  while($('#log').children.length>60)$('#log').lastChild.remove();
  clearTimeout(notify.timer);
  notify.timer=setTimeout(()=>box.hidden=true,bad?12000:6000);
}
async function run(fn) {
  try{
    await fn();
  }
  catch(e){
    notify(e.message,true);
  }
}
async function request(url, method='GET', body) {
  const options={
    method
  };
  if(body instanceof FormData)options.body=body;
  else if(body!==undefined){
    options.headers={
      'Content-Type':'application/json'
    };
    options.body=JSON.stringify(body);
  }
  const r=await fetch(url,options);
  const text=await r.text();
  let data;
  try{
    data=JSON.parse(text);
  }
  catch{
    data=text;
  }
  if(!r.ok){
    const d=data.detail;
    throw new Error(typeof d==='string'?d:JSON.stringify(d||data));
  }
  return data;
}
function row(target, cells) {
  const tr=el('tr');
  for(const cell of cells){
    const td=el('td');
    if(cell instanceof Node)td.append(cell);
    else if(Array.isArray(cell))td.append(...cell);
    else td.textContent=cell??'—';
    tr.append(td);
  }
  target.append(tr);
}
function empty(target, columns, message) {
  if(target.children.length)return;
  const tr=el('tr');
  const td=el('td',message,'empty-row');
  td.colSpan=columns;
  tr.append(td);
  target.append(tr);
}
function options(select, values, placeholder) {
  const prev=select.value;
  select.replaceChildren();
  if(placeholder)select.add(new Option(placeholder,''));
  for(const [value,text] of values)select.add(new Option(text,value));
  if([...select.options].some(o=>o.value===prev))select.value=prev;
}
function showPage(page) {
  if(!titles[page])page='map';
  if(page!==app.page && window.jobUI && !window.jobUI.canLeave())return;
  if(page!==app.page && window.pointEditor && !window.pointEditor.canLeave())return;
  if(page!==app.page && window.zoneEditor?.canLeave && !window.zoneEditor.canLeave())return;
  app.page=page;
  for(const n of document.querySelectorAll('.page'))n.hidden=n.id!==`page-${page}`;
  for(const n of document.querySelectorAll('nav button')){
    const active=n.dataset.page===page;
    n.classList.toggle('active',active);
    if(active)n.setAttribute('aria-current','page');
    else n.removeAttribute('aria-current');
    n.title=titles[n.dataset.page];
    n.setAttribute('aria-label',titles[n.dataset.page]);
  }
  $('#pageTitle').textContent=titles[page];
  document.title=`FMS · ${titles[page]}`;
  if(location.hash!==`#${page}`)history.replaceState(null,'',`#${page}`);
  if(page==='map')resize();
}
for(const b of document.querySelectorAll('[data-page],[data-goto]'))b.onclick=()=>showPage(b.dataset.page||b.dataset.goto);
window.addEventListener('hashchange',()=>showPage(location.hash.slice(1)));
$('#notice button').onclick=()=>$('#notice').hidden=true;
async function loadSites() {
  app.sites=await request('/api/v1/sites');
  options($('#siteSelect'),app.sites.map(s=>[s.site_id,s.name||s.site_id]),'현장 선택');
  renderSites();
}
async function selectSite(id) {
  if(window.pointEditor && !window.pointEditor.canLeave()){ $("#siteSelect").value=app.site||""; return; }
  if(window.jobUI && !window.jobUI.canLeave()){ $("#siteSelect").value=app.site||""; return; }
  window.jobUI?.resetSite();
  if(window.zoneEditor && !window.zoneEditor.canLeave()){
    $('#siteSelect').value=app.site||'';
    return;
  }
  const token=++app.siteToken;
  app.site=id||null;
  app.detail=null;
  app.locations=[];
  app.jobs=[];
  app.presets=[];
  app.steps=[];
  resetEdit();
  app.map=null;
  clearMap();
  renderAll();
  $('#siteSelect').value=id||'';
  if(!id)return;
  await refreshSite(token);
}
async function refreshSite(token=app.siteToken) {
  if(!app.site)return;
  const base=siteURL();
  const [detail,locs,jobs,presets]=await Promise.all([request(base),request(`${base}/locations`),request(`${base}/jobs`),request(`${base}/presets`)]);
  if(token!==app.siteToken)return;
  // 다른 사용자의 버전 변경이나 주기 갱신이 작성 중인 구역을 지우지 않게 한다.
  if(window.zoneEditor?.active || window.pointEditor?.active)return;
  app.detail=detail;
  app.locations=locs;
  app.jobs=jobs;
  refreshRobotInfo();
  app.presets=presets;
  window.jobUI?.setPresets(presets);
  const oldMap=app.map;
  const oldVersion=view.version;
  const found=detail.maps.find(m=>m.map_id===oldMap);
  app.map=found?oldMap:(detail.maps.find(m=>m.active_version)?.map_id||detail.maps[0]?.map_id||null);
  options($('#mapSelect'),detail.maps.map(m=>[m.map_id,m.map_id]),'맵 선택');
  $('#mapSelect').value=app.map||'';
  for(const s of document.querySelectorAll('.map-options'))options(s,detail.maps.map(m=>[m.map_id,m.map_id]));
  renderAll();
  const current=detail.maps.find(m=>m.map_id===app.map);
  if(oldMap!==app.map||oldVersion!==current?.active_version)await loadMap();
  else draw();
}
$('#siteSelect').onchange=()=>run(()=>selectSite($('#siteSelect').value));
$('#mapSelect').onchange=()=>run(async()=>{
  if(window.pointEditor && !window.pointEditor.canLeave()){ $("#mapSelect").value=app.map||""; return; }
  if(window.zoneEditor && !window.zoneEditor.canLeave()){
    $('#mapSelect').value=app.map||'';
    return;
  }
  app.map=$('#mapSelect').value||null;await loadMap();renderAnnotations();
});
$('#refresh').onclick=()=>run(async()=>{
  if(window.pointEditor && !window.pointEditor.canLeave())return;
  if(window.zoneEditor && !window.zoneEditor.canLeave())return;
  await loadSites();if(app.site&&!app.sites.some(s=>s.site_id===app.site))await selectSite(null);else await refreshSite();await pollFleet();
});
function renderAll() {
  renderSites();
  renderMaps();
  renderRobots();
  renderLocations();
  renderJobs();
  renderSteps();
  window.jobUI?.render();
  for(const f of document.querySelectorAll('#robotForm,#jobForm,#mapForm,#locForm'))for(const control of f.elements)control.disabled=!app.site;
}
function renderSites() {
  const tb=$('#siteRows');
  tb.replaceChildren();
  for(const s of app.sites)row(tb,[s.name,s.site_id,s.map_count,s.robot_count,[button('선택',()=>selectSite(s.site_id)),button('삭제',async()=>{
    if(!confirm(`현장 '${s.name||s.site_id}'과 등록 데이터를 삭제할까요?`))return;await request(`/api/v1/sites/${enc(s.site_id)}?force=true`,'DELETE');if(app.site===s.site_id)await selectSite(null);await loadSites();notify('현장을 삭제했습니다.');
  },'danger')]]);
  empty(tb,5,'등록된 현장이 없습니다. 위에서 첫 현장을 등록하세요.');
}
function renderMaps() {
  const tb=$('#mapRows');
  tb.replaceChildren();
  for(const m of app.detail?.maps||[]){
    const versions=el('div');
    for(const v of m.versions){
      if(v.is_active)versions.append(el('span',`${v.version} · 사용 중`,'ok'));
      else versions.append(button(v.version,async()=>{
        if(!confirm(`${m.map_id}의 ${v.version} 버전을 활성화할까요? 배정 로봇에 맵이 전송됩니다.`))return;await request(`${siteURL()}/maps/${enc(m.map_id)}/activate`,'POST',{
          version:v.version
        });await refreshSite();notify('맵 버전을 활성화했습니다.');
      }));
    }
    row(tb,[m.map_id,m.active_version||'활성 버전 없음',versions,button('맵 보기',async()=>{
      app.map=m.map_id;$('#mapSelect').value=m.map_id;showPage('map');await loadMap();
    })]);
  }
  empty(tb,4,app.site?'등록된 맵이 없습니다. 맵 파일을 업로드하세요.':'먼저 현장을 선택하세요.');
}
function renderRobots() {
  const tb=$('#robotRows');
  tb.replaceChildren();
  for(const assigned of app.detail?.robots||[]){
    const r=app.robots.get(assigned.robot_id),s=r?.state;
    const actions=[button('상세',()=>showRobot(assigned.robot_id))];
    actions.push(button('맵 재배정',async()=>{
      const map=prompt('배정할 맵 ID',assigned.map_id);if(!map)return;await request(`${siteURL()}/robots/${enc(assigned.robot_id)}`,'PUT',{
        map_id:map
      });await refreshSite();notify('로봇의 맵 배정을 변경했습니다.');
    }));
    actions.push(button('등록 해제',async()=>{
      if(!confirm(`${assigned.robot_id}의 현장 등록을 해제할까요?`))return;await request(`${siteURL()}/robots/${enc(assigned.robot_id)}`,'DELETE');await refreshSite();await loadSites();
    },'danger'));
    row(tb,[assigned.robot_id,label(r?.liveness)||'미접속',s?label(s.sub_state||s.state):'상태 수신 전',s?.battery?`${s.battery.charge.toFixed(0)}%`:'—',assigned.map_id,r?(r.map_synced?'일치':'불일치'):'확인 전',actions]);
  }
  empty(tb,7,app.site?'등록된 로봇이 없습니다.':'먼저 현장을 선택하세요.');
  const un=$('#unregistered');
  un.replaceChildren();
  for(const r of app.robots.values())if(!r.registered)un.append(el('p',`${r.robot_id} · ${label(r.liveness)}`));
  if(!un.children.length)un.append(el('p','미등록 로봇이 없습니다.','muted'));
  options($('#jobRobot'),(app.detail?.robots||[]).map(r=>[r.robot_id,r.robot_id]),'로봇 선택');
}
function poseText(p) {
  return `${Number(p.x).toFixed(2)} / ${Number(p.y).toFixed(2)} / ${Number(p.theta||0).toFixed(2)}`;
}
function renderLocations() {
  const tb=$('#locationRows');
  tb.replaceChildren();
  for(const l of app.locations)row(tb,[l.name,l.map_id,types[l.type]||l.type,poseText(l),[button('편집',()=>fillLocation(l,true)),button('삭제',async()=>{
    if(!confirm(`목적지 '${l.name}'을 삭제할까요?`))return;await request(`${siteURL()}/locations/${enc(l.name)}`,'DELETE');if(app.edit===l.name)resetEdit();await refreshSite();notify('목적지를 삭제했습니다.');
  },'danger')]]);
  empty(tb,5,app.site?'등록한 목적지가 없습니다.':'먼저 현장을 선택하세요.');
  options($('#jobTarget'),app.locations.map(l=>[l.name,`${l.name} (${l.map_id})`]),'목적지 선택');
  renderAnnotations();
}
function renderAnnotations() {
  const tb=$('#annotationRows');
  tb.replaceChildren();
  for(const l of view.annotations.locations||[])row(tb,[l.name,types[l.type]||l.type,poseText(l.pose),button('가져오기',()=>fillLocation({
    ...l.pose,name:l.name,type:l.type,map_id:app.map
  },false))]);
  empty(tb,4,'선택한 활성 맵에 포함된 위치가 없습니다.');
}
function fillLocation(l,edit) {
  resetEdit();
  const f=$('#locForm');
  for(const k of ['name','map_id','x','y','theta','type'])f.elements.namedItem(k).value=l[k]??0;
  app.edit=edit?l.name:null;
  f.elements.name.readOnly=false;
  $('#locFormTitle').textContent=edit?'목적지 편집':'맵 위치 가져오기';
  $('#cancelLocEdit').hidden=false;
  showPage('locations');
  f.elements.name.focus();
}
function resetEdit() {
  app.edit=null;
  $('#locForm').reset();
  $('#locForm').elements.name.readOnly=false;
  $('#locFormTitle').textContent='목적지 등록';
  $('#cancelLocEdit').hidden=true;
}
$('#cancelLocEdit').onclick=resetEdit;
function renderJobs() {
  const tb=$('#jobRows');
  tb.replaceChildren();
  for(const j of app.jobs){
    if(window.jobUI && !window.jobUI.visible(j))continue;
    const actions=[];
    if(j.status==='pending'){
      const choice=el('select');
      choice.setAttribute('aria-label',`${j.job_id}에 배정할 로봇`);
      options(choice,(app.detail?.robots||[]).map(r=>[r.robot_id,r.robot_id]),'로봇 지정');
      if(j.robot_id)choice.value=j.robot_id;
      actions.push(choice,button('배정',async()=>{
      const robot=choice.value;if(!robot)throw new Error('배정할 로봇을 선택하세요.');await request(`${siteURL()}/jobs/${enc(j.job_id)}/dispatch`,'POST',{
        robot_id:robot
      });await refreshSite();notify('작업을 실행했습니다.');
    }));
    }
    if(['assigned','working'].includes(j.status))actions.push(button('취소',async()=>{
      if(!confirm(`${j.job_id} 작업을 취소할까요?`))return;await request(`${siteURL()}/jobs/${enc(j.job_id)}/cancel`,'POST',{
      });await refreshSite();
    }));
    else if(j.status!=='canceling')actions.push(button('삭제',async()=>{
      if(!confirm(`${j.job_id} 작업을 삭제할까요?`))return;await request(`${siteURL()}/jobs/${enc(j.job_id)}`,'DELETE');await refreshSite();
    },'danger'));
    if(j.status==='pending')actions.push(button('취소',async()=>{await request(`${siteURL()}/jobs/${enc(j.job_id)}/cancel`,'POST',{});await refreshSite();}));
    row(tb,[j.preset_name?`${j.job_id} · ${j.preset_name}`:j.job_id,j.status==='pending'&&j.auto_assign?'자동 배정 대기':label(j.status),j.robot_id,j.commands.map(c=>window.jobUI?window.jobUI.describe(c):c.target).join(' → '),`${j.done_commands}/${j.total_commands}`,j.error||'—',actions]);
  }
  empty(tb,7,app.site?'작업이 없습니다. 목적지를 등록한 뒤 작업을 만드세요.':'먼저 현장을 선택하세요.');
}
function renderSteps() {
  const n=$('#jobSteps');
  n.replaceChildren(el('span',app.steps.length?app.steps.join(' → '):'선택한 목적지로 이동합니다.'));
  if(app.steps.length)n.append(button('비우기',()=>{
    app.steps=[];renderSteps();
  }));
}
$('#addStep').onclick=()=>{
  const target=$('#jobTarget').value;
  if(target){
    app.steps.push(target);
    renderSteps();
  }
};
function form(id, handler) {
  $(id).addEventListener('submit',e=>{
    e.preventDefault();run(async()=>{
      const submit=e.submitter;submit.disabled=true;try{
        await handler(new FormData(e.target));
      }
      finally{
        submit.disabled=false;
      }
    });
  });
}
form('#siteForm',async f=>{
  const site=await request('/api/v1/sites','POST',Object.fromEntries(f));$('#siteForm').reset();await loadSites();await selectSite(site.site_id);notify('현장을 등록했습니다.');
});
form('#robotForm',async f=>{
  await request(`${siteURL()}/robots`,'POST',Object.fromEntries(f));await refreshSite();await loadSites();notify('로봇을 등록했습니다.');
});
form('#mapForm',async f=>{
  f.set('activate',$('#mapForm').elements.activate.checked?'true':'false');const result=await request(`${siteURL()}/maps`,'POST',f);await refreshSite();await loadSites();notify(`맵을 업로드했습니다.${result.warnings?.length?' '+result.warnings.join(' / '):''}`);
});
form('#locForm',async f=>{
  const data=Object.fromEntries(f);
  for(const k of ['x','y','theta'])data[k]=Number(data[k]);
  const edit=app.edit;
  const result=await request(`${siteURL()}/locations${edit?'/'+enc(edit):''}`,edit?'PUT':'POST',data);
  if(edit){
    app.steps=app.steps.map(target=>target===edit?result.name:target);
    if(app.selection?.kind==='location' && app.selection.data.name===edit){
      app.selection=null;
      $('#objectInfo').hidden=true;
    }
  }
  resetEdit();
  await refreshSite();
  notify(`목적지를 저장했습니다.${result.warnings?.length?' '+result.warnings.join(' / '):''}`);
});
form('#jobForm',async f=>{
  const steps=app.steps.length?[...app.steps]:[f.get('target')].filter(Boolean);if(!steps.length)throw new Error('목적지를 선택하세요.');const data={
    commands:steps.map(target=>({
      type:'move',target
    }))
  };if(f.get('robot_id'))data.robot_id=f.get('robot_id');await request(`${siteURL()}/jobs`,'POST',data);app.steps=[];await refreshSite();notify('작업을 생성했습니다. 목록에서 실행할 수 있습니다.');
});
// 맵 픽셀은 원점이 왼쪽 위이며 world Y 와 같은 방향이다.
const canvas=$('#mapCanvas'),ctx=canvas.getContext('2d');
const view={
  image:null,meta:null,annotations:{
  },version:null,scale:1,tx:0,ty:0,w:0,h:0,hits:[],layers:{
    robots:true,zones:true,locations:true,docks:true,labels:true
  }
};
function clearMap() {
  window.pointEditor?.cancel();
  window.zoneEditor?.cancel();
  ++app.mapToken;
  view.image=null;
  view.meta=null;
  view.version=null;
  view.hits=[];
  view.annotations={
  };
  app.selection=null;
  $('#objectInfo').hidden=true;
  $('#mapCaption').textContent='선택한 맵 없음';
  $('#mapLive').textContent='';
  $('#mapScale').textContent='';
  $('#mapEmpty').hidden=false;
  $('#mapEmpty h2').textContent=app.site?'현장 맵을 불러오세요':'현장을 선택하세요';
  $('#mapEmpty p').textContent=app.site?'맵을 업로드하고 사용할 버전을 활성화하세요.':'상단에서 현장을 선택하거나 현장 설정에서 첫 현장을 등록하세요.';
  $('#mapEmpty button').dataset.goto=app.site?'maps':'settings';
  $('#mapEmpty button').textContent=app.site?'맵 관리 열기':'현장 설정 열기';
  renderAnnotations();
  draw();
}
async function loadMap() {
  clearMap();
  if(!app.map)return;
  const map=app.detail?.maps.find(m=>m.map_id===app.map);
  if(!map?.active_version){
    $('#mapEmpty h2').textContent='활성 맵 버전이 없습니다';
    $('#mapEmpty p').textContent='맵 관리에서 사용할 버전을 활성화하세요.';
    return;
  }
  const token=app.mapToken,base=mapURL(),version=map.active_version;
  const file=name=>`${base}/files/${enc(name)}?version=${enc(version)}`;
  $('#mapEmpty h2').textContent='맵을 불러오는 중';
  $('#mapEmpty p').textContent=app.map;
  try{
    const [meta,annotations]=await Promise.all([request(file('fms_map.json')),request(file('annotations.json'))]);
    const image=new Image();
    await new Promise((resolve,reject)=>{
      image.onload=resolve;image.onerror=()=>reject(new Error('맵 이미지를 불러오지 못했습니다.'));image.src=file(meta.image);
    });
    if(token!==app.mapToken)return;
    if(!Number.isFinite(meta.resolution)||meta.resolution<=0||!Number.isFinite(meta.origin_x)||!Number.isFinite(meta.origin_y))throw new Error('맵 좌표 정보가 올바르지 않습니다.');
    Object.assign(view,{
      image,meta,annotations,version
    });
    $('#mapEmpty').hidden=true;
    $('#mapCaption').textContent=app.map;
    fit();
    renderAnnotations();
  }
  catch(e){
    if(token!==app.mapToken)return;
    $('#mapEmpty h2').textContent='맵을 불러오지 못했습니다';
    $('#mapEmpty p').textContent=e.message;
    throw e;
  }
}
function resize() {
  const rect=$('#mapStage').getBoundingClientRect();
  if(!rect.width||!rect.height)return;
  const wasEmpty=!view.w;
  const cx=view.w/2,cy=view.h/2;
  view.tx+=(rect.width/2-cx);
  view.ty+=(rect.height/2-cy);
  view.w=rect.width;
  view.h=rect.height;
  const dpr=window.devicePixelRatio||1;
  canvas.width=Math.round(rect.width*dpr);
  canvas.height=Math.round(rect.height*dpr);
  ctx.setTransform(dpr,0,0,dpr,0,0);
  if(wasEmpty&&view.image)fit();
  else draw();
}
function fit() {
  if(!view.image||!view.w)return;
  view.scale=Math.min((view.w-80)/view.image.width,(view.h-120)/view.image.height);
  view.tx=(view.w-view.image.width*view.scale)/2;
  view.ty=(view.h-view.image.height*view.scale)/2;
  draw();
}
function point(p) {
  const m=view.meta;
  return {
    x:view.tx+(p.x-m.origin_x)/m.resolution*view.scale,y:view.ty+(p.y-m.origin_y)/m.resolution*view.scale
  };
}
function zoom(factor,x=view.w/2,y=view.h/2) {
  if(!view.image)return;
  const next=Math.max(.01,Math.min(30,view.scale*factor));
  const ratio=next/view.scale;
  view.tx=x-(x-view.tx)*ratio;
  view.ty=y-(y-view.ty)*ratio;
  view.scale=next;
  draw();
}
function mapRobots() {
  const m=app.detail?.maps.find(m=>m.map_id===app.map);
  if(!m||!view.image)return [];
  return [...app.robots.values()].filter(r=>r.state?.pose&&r.state.map_id===m.map_uid&&r.state.map_version===view.version);
}
function isFresh(r) {
  return ['ONLINE','UNREGISTERED'].includes(r.liveness)&&r.last_state_at&&Date.now()/1000-r.last_state_at<=3;
}
function text(t,x,y,color='#dbe7f5') {
  ctx.font='12px "Apple SD Gothic Neo", sans-serif';
  ctx.lineWidth=4;
  ctx.strokeStyle='#0d131ce8';
  ctx.strokeText(t,x,y);
  ctx.fillStyle=color;
  ctx.fillText(t,x,y);
}
function draw() {
  ctx.clearRect(0,0,view.w,view.h);
  if(!view.image||!view.meta)return;
  ctx.imageSmoothingEnabled=false;
  ctx.drawImage(view.image,view.tx,view.ty,view.image.width*view.scale,view.image.height*view.scale);
  view.hits=[];
  if(view.layers.zones)for(const z of view.annotations.zones||[]){
    if(window.zoneEditor?.editingId===z.zone_id)continue;
    const pts=z.polygon.map(point);
    if(pts.length<3)continue;
    ctx.beginPath();
    pts.forEach((p,i)=>i?ctx.lineTo(p.x,p.y):ctx.moveTo(p.x,p.y));
    ctx.closePath();
    const color=z.type==='elevator'?'#ab9aee':z.type==='slow'?'#d6ad59':z.type==='oneway'?'#77a9e4':'#e57982';
    ctx.fillStyle=color+'32';
    ctx.fill();
    ctx.strokeStyle=color;
    ctx.lineWidth=1.5;
    ctx.stroke();
    view.hits.push({
      kind:'zone',data:z,pts
    });
    if(view.layers.labels&&view.scale/view.meta.resolution>12)text(z.name||label(z.type),pts[0].x+6,pts[0].y+16,color);
  }
  const locs=[...(view.annotations.locations||[]).map(l=>({
    ...l,source:'맵',pose:l.pose
  })),...app.locations.filter(l=>l.map_id===app.map).map(l=>({
    ...l,source:'등록',pose:l
  }))];
  for(const l of locs){
    const dock=['charger','parking'].includes(l.type);
    if(!(dock?view.layers.docks:view.layers.locations))continue;
    const p=point(l.pose),selected=app.selection?.key===`location:${l.source}:${l.name}`;
    const color=l.type==='charger'?'#77d8c3':l.type==='parking'?'#b3a1ed':'#e2bd79';
    ctx.save();
    ctx.translate(p.x,p.y);
    ctx.strokeStyle=color;
    ctx.fillStyle='#152334';
    ctx.lineWidth=selected?3:2;
    if(dock){
      ctx.beginPath();
      ctx.rect(-8,-8,16,16);
      ctx.fill();
      ctx.stroke();
      text(l.type==='charger'?'ϟ':'P',-4,4,color);
    }
    else{
      ctx.rotate(Math.PI/4);
      ctx.fillRect(-5,-5,10,10);
      ctx.strokeRect(-5,-5,10,10);
      ctx.rotate(-Math.PI/4);
    }
    ctx.rotate(l.pose.theta||0);
    ctx.beginPath();
    ctx.moveTo(10,0);
    ctx.lineTo(23,0);
    ctx.lineTo(18,-4);
    ctx.moveTo(23,0);
    ctx.lineTo(18,4);
    ctx.stroke();
    ctx.restore();
    if(view.layers.labels&&(dock||view.scale/view.meta.resolution>16||selected))text(l.name,p.x+13,p.y-12,color);
    view.hits.push({
      kind:'location',data:l,x:p.x,y:p.y,r:15
    });
  }
  const robots=mapRobots();
  for(const r of robots){
    if(!view.layers.robots)break;
    const p=point(r.state.pose),fresh=isFresh(r),bad=['ERROR','EMERGENCY'].includes(r.state.state);
    const color=bad?'#f2848c':r.state.state==='BUSY'?'#6aabff':'#77d8c3';
    const size=Math.max(12,Math.min(34,(r.state.width||.5)/view.meta.resolution*view.scale/2));
    ctx.save();
    ctx.globalAlpha=fresh?1:.45;
    ctx.translate(p.x,p.y);
    if(app.selection?.key===`robot:${r.robot_id}`){
      ctx.beginPath();
      ctx.arc(0,0,size+7,0,Math.PI*2);
      ctx.strokeStyle='#fff';
      ctx.lineWidth=1;
      ctx.stroke();
    }
    ctx.rotate(r.state.pose.theta||0);
    ctx.fillStyle='#17324b';
    ctx.strokeStyle=color;
    ctx.lineWidth=2;
    ctx.beginPath();
    ctx.roundRect(-size,-size*.7,size*2,size*1.4,4);
    ctx.fill();
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(size-3,0);
    ctx.lineTo(size-10,-5);
    ctx.lineTo(size-10,5);
    ctx.closePath();
    ctx.fillStyle=color;
    ctx.fill();
    ctx.restore();
    text(`${r.robot_id}${fresh?'':' · 수신 지연'}`,p.x+size+8,p.y+4,fresh?color:'#c5ceda');
    view.hits.push({
      kind:'robot',data:r,x:p.x,y:p.y,r:size+8
    });
  }
  $('#mapLive').textContent=`로봇 ${robots.length}대${view.layers.robots?'':' · 로봇 표시 꺼짐'}`;
  const ppm=view.scale/view.meta.resolution;
  const meters=Math.pow(10,Math.floor(Math.log10(90/ppm)));
  const width=meters*ppm;
  $('#mapScale').style.width=`${width}px`;
  $('#mapScale').textContent=`${meters} m`;
  window.zoneEditor?.draw();
  window.pointEditor?.draw();
}
function selectedRobotJob(robot) {
  const active=['assigned','working','canceling'];
  return app.jobs.find(j=>j.robot_id===robot.robot_id&&active.includes(j.status)&&j.order_id===robot.state?.order_id)
    || app.jobs.find(j=>j.robot_id===robot.robot_id&&active.includes(j.status));
}
function refreshRobotInfo() {
  if(app.selection?.kind!=='robot'||$('#objectInfo').hidden)return;
  const id=app.selection.data.robot_id;
  const robot=app.robots.get(id)||{robot_id:id,liveness:'LOST',state:null};
  window.robotDetails.render($('#objectContent'),robot,selectedRobotJob(robot));
}
function showRobot(id) {
  const r=app.robots.get(id);
  if(!r){
    notify('아직 로봇 상태를 수신하지 못했습니다.');
    return;
  }
  showObject({
    kind:'robot',data:r
  });
}
function showObject(hit) {
  const box=$('#objectContent');
  box.replaceChildren();
  const d=hit.data;
  app.selection={
    ...hit,key:hit.kind==='robot'?`robot:${d.robot_id}`:hit.kind==='location'?`location:${d.source}:${d.name}`:`zone:${d.zone_id}`
  };
  $('#objectInfo').hidden=false;
  $('#objectInfo').classList.toggle('robot-popup',hit.kind==='robot');
  if(hit.kind==='robot'){
    window.robotDetails.render(box,d,selectedRobotJob(d));
    if(app.page!=='map'){
      const dialog=document.createElement('dialog');
      dialog.className='panel robot-dialog';
      dialog.append(button('닫기',()=>dialog.close()),...box.childNodes);
      document.body.append(dialog);
      dialog.addEventListener('close',()=>dialog.remove());
      dialog.showModal();
      $('#objectInfo').hidden=true;
    }
  }
  else if(hit.kind==='location'){
    box.append(el('h3',d.name),el('p',types[d.type]||d.type),el('p',`좌표: ${poseText(d.pose)}`),el('p',d.source==='등록'?'관리자가 등록한 목적지':'맵에 포함된 원본 위치'),button(d.source==='등록'?'목적지 편집':'목적지로 가져오기',()=>fillLocation({
      ...d.pose,name:d.name,type:d.type,map_id:app.map
    },d.source==='등록')));
  }
  else {
    box.append(el('h3',d.name||'이름 없는 구역'),el('p',({
      prohibit:'주행금지구역 (Forbidden zone)',elevator:'엘리베이터 구역 · 위치 표시용',slow:'감속 구역 · 표시 전용',oneway:'일방통행 구역 · 표시 전용'
    })[d.type]||d.type));
    window.zoneEditor?.actions(box,d);
  }
  draw();
}
$('#closeInfo').onclick=()=>{
  app.selection=null;
  $('#objectInfo').hidden=true;
  draw();
};
let drag=null;
canvas.onpointerdown=e=>{
  if(e.button!==0)return;
  if(window.pointEditor?.pointerDown(e))return;
  if(window.zoneEditor?.pointerDown(e))return;
  drag={
    x:e.clientX,y:e.clientY,tx:view.tx,ty:view.ty,moved:false
  };
  canvas.setPointerCapture(e.pointerId);
};
canvas.onpointermove=e=>{
  if(window.pointEditor?.pointerMove(e))return;
  if(window.zoneEditor?.pointerMove(e))return;
  if(!drag)return;
  const dx=e.clientX-drag.x,dy=e.clientY-drag.y;
  if(Math.hypot(dx,dy)>4)drag.moved=true;
  view.tx=drag.tx+dx;
  view.ty=drag.ty+dy;
  draw();
};
canvas.onpointerup=e=>{
  if(window.pointEditor?.pointerUp(e))return;
  if(window.zoneEditor?.pointerUp(e))return;
  if(!drag)return;
  const click=!drag.moved;
  drag=null;
  canvas.releasePointerCapture(e.pointerId);
  if(!click)return;
  const rect=canvas.getBoundingClientRect(),p={
    x:e.clientX-rect.left,y:e.clientY-rect.top
  };
  const hit=[...view.hits].reverse().find(h=>h.pts?inside(p,h.pts):Math.hypot(p.x-h.x,p.y-h.y)<h.r);
  if(hit)showObject(hit);
  else $('#closeInfo').click();
};
canvas.onpointercancel=()=>{
  drag=null;
  window.zoneEditor?.pointerCancel();
  window.pointEditor?.pointerCancel();
};
function inside(p,pts) {
  let result=false;
  for(let i=0,j=pts.length-1;i<pts.length;j=i++){
    const a=pts[i],b=pts[j];
    if((a.y>p.y)!==(b.y>p.y)&&p.x<(b.x-a.x)*(p.y-a.y)/(b.y-a.y)+a.x)result=!result;
  }
  return result;
}
canvas.addEventListener('wheel',e=>{
  e.preventDefault();const r=canvas.getBoundingClientRect();zoom(Math.exp(-e.deltaY*.0015),e.clientX-r.left,e.clientY-r.top);
},{
  passive:false
});
$('#mapStage').onkeydown=e=>{
  if(e.target!==$('#mapStage'))return;
  if(e.key==='+'||e.key==='=')zoom(1.3);
  else if(e.key==='-')zoom(1/1.3);
  else if(e.key==='0')fit();
  else if(e.key==='Escape'){
    if(window.pointEditor?.active)window.pointEditor.canLeave();
    else if(window.zoneEditor?.active)window.zoneEditor.canLeave();
    else $('#closeInfo').click();
  }
};
$('#zoomIn').onclick=()=>zoom(1.3);
$('#zoomOut').onclick=()=>zoom(1/1.3);
$('#fitMap').onclick=fit;
for(const input of document.querySelectorAll('[data-layer]'))input.onchange=()=>{
  view.layers[input.dataset.layer]=input.checked;
  $('#closeInfo').click();
};
new ResizeObserver(resize).observe($('#mapStage'));
// WebSocket 으로 즉시 갱신하고 REST 스냅샷으로 liveness 및 등록 변경을 보완한다.
let fleetBusy=false,siteBusy=false;
async function pollFleet() {
  if(fleetBusy)return;
  fleetBusy=true;
  try{
    const body=await request('/api/v1/robots');
    app.robots=new Map(body.robots.map(r=>[r.robot_id,r]));
    renderRobots();
    refreshRobotInfo();
    draw();
  }
  finally{
    fleetBusy=false;
  }
}
function connectStream() {
  const ws=new WebSocket(`${location.protocol==='https:'?'wss:':'ws:'}//${location.host}/api/v1/robots/stream`);
  ws.onmessage=e=>{
    try{
      const data=JSON.parse(e.data);
      if(data.type==='snapshot')app.robots=new Map(data.robots.map(r=>[r.robot_id,r]));
      else if(data.type==='event'){
        if(data.robot)app.robots.set(data.robot_id,data.robot);
        else app.robots.delete(data.robot_id);
      }
      if(app.page==='robots')renderRobots();
      refreshRobotInfo();
      draw();
    }
    catch(err){
      console.error('로봇 상태 수신 실패',err);
    }
  };
  ws.onclose=()=>setTimeout(connectStream,3000);
  ws.onerror=()=>ws.close();
}
async function health() {
  try{
    const h=await request('/health');
    $('#serverDot').classList.add('online');
    $('#serverStatus').textContent=h.mqtt_connected?'서버 · MQTT 연결됨':'서버 연결 · MQTT 대기';
    $('#healthDetail').textContent=`서버 정상 / MQTT ${h.mqtt_connected?'연결됨':'연결 대기'} / 수신 로봇 ${h.robots_known}대`;
  }
  catch{
    $('#serverDot').classList.remove('online');
    $('#serverStatus').textContent='서버 연결 끊김';
    $('#healthDetail').textContent='서버에 연결할 수 없습니다.';
  }
}
showPage(location.hash.slice(1)||'map');
renderAll();
run(async()=>{
  await loadSites();
  if(app.sites.length)await selectSite(app.sites[0].site_id);
  else clearMap();
  await pollFleet();
});
health();
connectStream();
setInterval(()=>{
  health();run(pollFleet);
},3000);
setInterval(()=>{
  if(!app.site||siteBusy)return;siteBusy=true;run(async()=>{
    try{
      await refreshSite();
    }
    finally{
      siteBusy=false;
    }
  });
},6000);

// 수신이 끊겨도 열린 팝업의 마지막 수신 경과 시간은 갱신한다.
setInterval(refreshRobotInfo,1000);
