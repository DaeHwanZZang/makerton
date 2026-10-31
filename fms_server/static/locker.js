const $ = id => document.getElementById(id);
const parcelStates = {planned:'적재 예정',loading:'적재 중',onboard:'로봇 적재',unloading:'하차 중',delivered:'배송 완료',returned:'반납 완료',canceled:'미적재 종료',unknown:'확인 필요'};
let locations = [], stations = [], lastDeliveries = '', lastParcels = '', returning = null;
const openSteps = new Set();
async function api(path, method='GET', body) {
  const response = await fetch(`/api/v1/${path}`, {method, headers:{'Content-Type':'application/json'}, body:body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
  return data;
}
function option(select, value, label) { const o=document.createElement('option');o.value=value;o.textContent=label;select.append(o); }
function locationOptions(select, value='') { option(select,'','목적지 선택');locations.forEach(l=>option(select,l.name,`${l.name} (${l.map_id})`));select.value=value; }
async function loadLocations() {
  const site = $('site').value ? `sites/${encodeURIComponent($('site').value)}` : null;
  [locations, stations] = site ? await Promise.all([api(`${site}/locations`), api(`${site}/pickup-stations`)]) : [[], []];
  // 픽업 지점은 세대 목적지 후보에서 뺀다.
  // 충전소는 반납 후 도킹 대상으로 고른다. 맵에 물리 충전기가 있는 충전소만 서버가 받아들인다.
  const chargers = locations.filter(l => l.type === 'charger');
  $('charger').replaceChildren(); option($('charger'),'auto','자동 (반납 지점에서 가까운 빈 충전소)'); option($('charger'),'off','도킹 안 함');
  chargers.forEach(c => option($('charger'), c.name, `${c.name} (${c.map_id})`));
  locations = locations.filter(l => !['pickup', 'tray_return'].includes(l.type));
  $('pickup').replaceChildren(); option($('pickup'),'','픽업스테이션 선택');
  stations.forEach(s => option($('pickup'), s.station_id, `${s.name || s.station_id} (${s.map_id})${s.status==='ok'?'':' · 연결 확인 필요'}`));
  $('mappings').replaceChildren();
}
function addMapping(key='', target='') {
  const row=document.createElement('tr'), keyCell=document.createElement('td'), targetCell=document.createElement('td'), actions=document.createElement('td');
  const input=document.createElement('input');input.value=key;input.placeholder='1-305';input.pattern='[0-9]+-[0-9]+';input.required=true;input.setAttribute('aria-label','세대 건물-호실');keyCell.append(input);
  const select=document.createElement('select');select.required=true;select.setAttribute('aria-label','세대 목적지');locationOptions(select,target);targetCell.append(select);
  const remove=document.createElement('button');remove.type='button';remove.textContent='제거';remove.onclick=()=>row.remove();actions.append(remove);
  row.append(keyCell,targetCell,actions);$('mappings').append(row);
}
$('site').onchange=()=>loadLocations().then(()=>addMapping()).catch(showError);
$('addMapping').onclick=()=>addMapping();
function showError(error) { $('message').textContent=error.message; }
$('configForm').onsubmit=async event=>{
  event.preventDefault();
  try {
    const households={};
    for(const row of $('mappings').rows) {
      const key=row.querySelector('input').value.trim();
      if (Object.hasOwn(households,key)) throw new Error(`세대 ${key}가 중복되었습니다.`);
      households[key]=row.querySelector('select').value;
    }
    await api('locker/config','PUT',{site_id:$('site').value,pickup_station_id:$('pickup').value,households,pick_duration:Number($('pickDuration').value),drop_duration:Number($('dropDuration').value),charger_target:$('charger').value,charge_min:Number($('chargeMin').value)});
    $('legacyPickup').hidden=true;
    $('message').textContent='목적지 연결을 저장했습니다. 대기 중인 배송부터 적용됩니다.';
  } catch(error) { showError(error); }
};
function cell(row, value) { const td=document.createElement('td');td.textContent=value ?? '—';row.append(td);return td; }
function openReturn(d) {
  returning=d.job_id;$('receipts').replaceChildren();$('confirmOnboard').checked=false;$('returnError').textContent='';
  for (const item of d.items) {
    const label=document.createElement('label');label.className='receipt';
    const input=document.createElement('input');input.type='checkbox';input.value=item.receipt_no;input.checked=d.results[item.receipt_no]==='DELIVERED';input.disabled=input.checked;
    label.append(input,document.createTextNode(`${item.receipt_no} · ${item.dong}동 ${item.ho}호 — 인도 완료`));$('receipts').append(label);
  }
  $('returnDialog').showModal();
}
$('closeDialog').onclick=()=>$('returnDialog').close();
$('returnForm').onsubmit=async event=>{
  event.preventDefault();
  try {
    await api(`locker/deliveries/${encodeURIComponent(returning)}/return`,'POST',{delivered_receipts:[...$('receipts').querySelectorAll('input:checked')].map(el=>el.value),confirm_remaining_onboard:$('confirmOnboard').checked});
    $('returnDialog').close();await refresh();
  } catch(error) { $('returnError').textContent=error.message; }
};
async function refresh() {
  const data=await api('locker');
  const serialized=JSON.stringify(data.deliveries);
  if(serialized!==lastDeliveries) {
    lastDeliveries=serialized;$('deliveries').replaceChildren();
    for(const d of [...data.deliveries].reverse()) {
      const row=document.createElement('tr');cell(row,`${d.job_id} / ${d.child_id || '—'}`);cell(row,d.robot_id);
      // 배송 1건 = 픽업→적재→세대 하차→트레이 반납. 단계 목록은 FMS 계산값(관제 화면과 같음).
      const stage=cell(row,window.deliveryView.summary(d));
      if(d.progress){const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='단계 보기';details.open=openSteps.has(d.job_id);details.ontoggle=()=>details.open?openSteps.add(d.job_id):openSteps.delete(d.job_id);details.append(summary,window.deliveryView.steps(d.progress));stage.append(details);}
      const bundle=cell(row,d.bundle?`${d.bundle.bundle_id} · ${d.bundle.summary} ${d.bundle.progress}`:`예상 ${(d.proposed_items||[]).length}건`);
      for(const item of d.bundle?.items||[]){const small=document.createElement('small');small.textContent=`${item.tray_id} · ${item.parcel_id} · ${item.receipt_no} → ${item.household} (${item.target}) · ${parcelStates[item.status]||item.status}`;bundle.append(small);}
      const info=cell(row,d.error || '');
      if(d.phase==='blocked') {const button=document.createElement('button');button.textContent='적재물 확인·복귀';button.onclick=()=>openReturn(d);info.append(button);}
      if(d.phase==='queued'&&!d.robot_id) {
        // 로봇 배정 전 취소. 세대 연결이 없는 물품은 보관소가 직접 수령으로 바꾸고 나머지로 새 배송을 만든다.
        const button=document.createElement('button');button.textContent='배송 취소';
        button.onclick=async()=>{
          const reason=prompt(`배송 ${d.job_id}를 취소할까요? 목적지 연결이 없는 세대 물품은 직접 수령으로 바뀝니다.\n취소 사유 (선택):`,d.error||'');
          if(reason===null)return;
          try{const r=await api(`locker/deliveries/${encodeURIComponent(d.job_id)}/cancel`,'POST',{reason});
            $('message').textContent=`배송 ${d.job_id}를 취소했습니다${r.unroutable.length?` (직접 수령 전환 ${r.unroutable.join(', ')})`:''}.`;
            lastDeliveries='';await refresh();}catch(error){showError(error);}
        };
        info.append(button);
      }
      $('deliveries').append(row);
    }
  }
  return data;
}
function where(p) {
  const l=p.last_confirmed_location||{};
  if(l.kind==='robot')return `로봇 ${l.robot_id} ${l.tray_id}`;
  if(l.kind==='floor')return `바닥 · ${l.target} (${l.map_id} ${Number(l.x).toFixed(1)}, ${Number(l.y).toFixed(1)})`;
  if(l.kind==='station')return `픽업스테이션 ${l.name||l.station_id||''}`;
  return '보관소';
}
async function resolveParcel(p) {
  const choices={onboard:'로봇 트레이에 있음',delivered:'세대에 인도함',returned:'보관소에 돌려놓음',not_loaded:'실리지 않음'};
  const result=prompt(`${p.parcel_id} 실물 위치를 입력하세요: ${Object.entries(choices).map(([k,v])=>`${k}=${v}`).join(', ')}`);
  if(!result)return;
  const reason=prompt('정정 사유');
  if(!reason)return;
  await api(`sites/${encodeURIComponent(p.site_id)}/parcels/${encodeURIComponent(p.parcel_id)}/resolve`,'POST',{result:result.trim(),reason});
  lastParcels='';await refreshParcels();
}
async function refreshParcels() {
  if(!$('site').value)return;
  const parcels=await api(`sites/${encodeURIComponent($('site').value)}/parcels`);
  const serialized=JSON.stringify(parcels);
  if(serialized===lastParcels)return;
  lastParcels=serialized;$('parcels').replaceChildren();
  for(const p of [...parcels].reverse()){
    const row=document.createElement('tr');
    const id=cell(row,p.parcel_id);const small=document.createElement('small');small.textContent=`${p.source.locker_id} · ${p.source.receipt_no}`;id.append(small);
    cell(row,p.household);const state=cell(row,parcelStates[p.status]||p.status);if(p.status==='unknown')state.className='warn';
    cell(row,p.assigned_robot_id?`${p.assigned_robot_id} · ${p.tray_id}`:'—');cell(row,where(p));
    const actions=cell(row,'');
    if(['unknown','onboard','planned','loading','unloading'].includes(p.status)){const b=document.createElement('button');b.textContent='실물 확인·정정';b.onclick=()=>resolveParcel(p).catch(showError);actions.append(b);}
    $('parcels').append(row);
  }
}
async function init() {
  const [sites,data]=await Promise.all([api('sites'),refresh()]);
  option($('site'),'','현장 선택');sites.forEach(s=>option($('site'),s.site_id,s.name || s.site_id));
  if(data.config) {
    $('site').value=data.config.site_id;await loadLocations();$('pickup').value=data.config.pickup_station_id||'';
    if(data.config.pickup_target&&!data.config.pickup_station_id){$('legacyPickup').hidden=false;$('legacyPickup').textContent=`이전 설정은 일반 목적지 '${data.config.pickup_target}'를 보관소로 씁니다. 픽업스테이션을 선택해 저장하면 전환됩니다.`;}
    $('pickDuration').value=data.config.pick_duration;$('dropDuration').value=data.config.drop_duration;
    $('charger').value=data.config.charger_target||'auto';$('chargeMin').value=data.config.charge_min??30;
    Object.entries(data.config.households).forEach(([key,target])=>addMapping(key,target));
  } else addMapping();
  await refreshParcels();
  setInterval(()=>refresh().then(refreshParcels).catch(showError),3000);
}
init().catch(showError);
