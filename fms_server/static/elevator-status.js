'use strict';
// 운행 상태만 주기 갱신한다. 아래 연결 편집 폼과 선택한 층/구역은 다시 그리지 않는다.
(() => {
  let siteKey=null;
  const pending=new Set(),rows=new Map();
  const body=$('#elevatorStatusRows'),notice=$('#elevatorStatusNotice');
  const set=(node,value)=>{const text=String(value);if(node.textContent!==text)node.textContent=text;};
  const doorNames={CLOSED:'닫힘',OPENING:'열리는 중',OPEN:'열림',CLOSING:'닫히는 중'};
  function operation(s){
    if(s.moving)return '이동 중';
    if(s.door==='OPENING')return '문 여는 중';
    if(s.door==='CLOSING')return '문 닫는 중';
    if(s.door==='OPEN')return s.held?'문 열림 유지':'승하차 대기';
    if(s.door==='CLOSED')return '대기';
    return '상태 확인 필요';
  }
  function create(e){
    const tr=el('tr'),identity=el('td'),name=el('strong'),id=el('small',undefined,'elev-live-meta');
    identity.append(name,id);
    const floorCell=el('td'),floor=el('strong',undefined,'elev-live-floor'),last=el('small','마지막 수신값','elev-live-meta');
    floorCell.append(floor,last);
    const statusCell=el('td'),status=el('span',undefined,'elev-live-state');statusCell.append(status);
    const target=el('td'),door=el('td'),connectionCell=el('td'),connection=el('span'),updated=el('small',undefined,'elev-live-meta');
    connectionCell.append(connection,updated);
    tr.append(identity,floorCell,statusCell,target,door,connectionCell);
    const row={tr,name,id,floor,last,status,target,door,connection,updated};rows.set(e.elevator_id,row);return row;
  }
  function render(data){
    const entries=data.elevators;
    if(!Array.isArray(entries))throw new Error('승강기 상태 목록이 올바르지 않습니다.');
    $('#elevatorStatusSource').textContent=data.simulation_enabled?'내장 시뮬레이션':'승강기 제어기';
    const ids=new Set(entries.map(e=>e.elevator_id));
    for(const [id,row] of rows)if(!ids.has(id)){row.tr.remove();rows.delete(id);}
    if(!rows.size)body.replaceChildren();
    for(const e of entries){
      const row=rows.get(e.elevator_id)||create(e),s=e.runtime||{};
      const known=Number.isFinite(s.received_at),fresh=known&&s.liveness==='ONLINE';
      row.hasState=known;
      row.tr.dataset.fresh=fresh?'true':'false';
      set(row.name,e.name||e.elevator_id);set(row.id,e.elevator_id);
      // 이동 중 at=null이다. 임의로 출발층/도착층을 현재 층이라고 표시하지 않는다.
      set(row.floor,known?(s.moving?'층간 이동':s.current_floor||s.at||'—'):'—');
      row.last.hidden=!known||fresh;
      set(row.status,!known?'상태 미수신':`${fresh?'':'마지막: '}${operation(s)}`);
      row.status.dataset.tone=!fresh?'unknown':s.moving?'moving':s.door==='OPEN'?'open':'idle';
      const target=s.target_floor||s.target;
      set(row.target,known&&target?`${target}${!s.moving&&s.at===s.target?' · 도착':''}`:'—');
      set(row.door,known?doorNames[s.door]||s.door||'—':'—');
      set(row.connection,!known?'미수신':fresh?'수신 중':'수신 지연');
      row.connection.className=fresh?'ok':'warn';
      if(known){
        const age=Math.max(0,Math.floor(Date.now()/1000-s.received_at));
        set(row.updated,age<1?'방금':age<60?`${age}초 전`:`${Math.floor(age/60)}분 전`);
      }else set(row.updated,'시뮬레이터/제어기 연결을 확인하세요.');
      body.append(row.tr);
    }
    empty(body,6,'등록된 승강기가 없습니다. 아래에서 층별 구역을 연결하세요.');
    notice.hidden=true;
  }
  async function poll(){
    if(app.page!=='elevators')return;
    const site=app.site;
    if(siteKey!==site){
      siteKey=site;rows.clear();body.replaceChildren();notice.hidden=true;
      $('#elevatorStatusSource').textContent='상태 확인 중';
    }
    if(!site){empty(body,6,'현장을 먼저 선택하세요.');return;}
    if(pending.has(site))return;
    pending.add(site);
    try{
      const data=await request(`/api/v1/sites/${enc(site)}/elevators`);
      if(site!==app.site||app.page!=='elevators')return;
      render(data);
    }catch(e){
      if(site!==app.site||app.page!=='elevators')return;
      notice.hidden=false;set(notice,'최신 운행 상태를 확인할 수 없습니다. 서버 연결을 확인하세요.');
      for(const row of rows.values()){
        row.tr.dataset.fresh='false';row.last.hidden=!row.hasState;
        set(row.status,'최신 상태 확인 불가');row.status.dataset.tone='unknown';
        set(row.connection,'연결 확인 필요');row.connection.className='warn';
      }
    }finally{pending.delete(site);}
  }
  setInterval(poll,1000);
  $('#refresh').addEventListener('click',poll);
  poll();
})();
