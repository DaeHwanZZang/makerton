'use strict';
// 읽기 전용 로봇 상세. 수신 갱신 시 접힘 상태·스크롤·키보드 포커스를 유지한다.
window.robotDetails = (()=>{
  const cards=new WeakMap();
  const number=(n,places=2,unit='')=>Number.isFinite(n)?`${n.toFixed(places)}${unit}`:'—';
  const set=(node,value)=>{const text=String(value);if(node.textContent!==text)node.textContent=text;};
  const statusNames={IDLE:'대기',BUSY:'작업 중',MOVING:'이동 중',WAITING:'대기 중',ACTING:'동작 수행 중',CHARGING:'충전 중',ERROR:'오류',EMERGENCY:'비상정지'};
  const connectionNames={ONLINE:'연결됨',STALE:'수신 지연',OFFLINE:'연결 끊김',LOST:'연결 유실',UNREGISTERED:'미등록',UNKNOWN:'연결 확인 중'};
  function create(container){
    const root=el('article',undefined,'robot-detail');
    const header=el('header',undefined,'rd-header'), glyph=el('span','▸','rd-glyph');
    glyph.setAttribute('aria-hidden','true');
    const identity=el('div'),name=el('h3',undefined,'rd-name'),connection=el('div',undefined,'rd-connection');
    identity.append(name,connection);header.append(glyph,identity);
    const status=el('span',undefined,'rd-status');status.setAttribute('role','status');
    const notice=el('p',undefined,'rd-notice');
    const battery=el('section',undefined,'rd-battery');
    const batteryLine=el('div',undefined,'rd-battery-line'),charge=el('strong'),chargeState=el('span');
    batteryLine.append(charge,chargeState);
    const batteryTrack=el('div',undefined,'rd-battery-track'),batteryFill=el('span');
    batteryTrack.setAttribute('role','meter');batteryTrack.setAttribute('aria-label','배터리 잔량');
    batteryTrack.setAttribute('aria-valuemin','0');batteryTrack.setAttribute('aria-valuemax','100');
    batteryTrack.append(batteryFill);battery.append(batteryLine,batteryTrack);
    const task=el('section',undefined,'rd-task');
    const taskLabel=el('div','현재 작업','rd-label'),taskTitle=el('div',undefined,'rd-task-title'),taskStep=el('p');
    const progress=el('progress');progress.setAttribute('aria-label','작업 단계 진행률');
    task.append(taskLabel,taskTitle,taskStep,progress);
    // 직사각형 적재부를 둘로 나눈 두 트레이. FMS 장부(액션 완료 보고 기준)의 값이다.
    const cargo=el('section',undefined,'rd-cargo');cargo.setAttribute('aria-label','적재 트레이');
    const facts=el('dl',undefined,'rd-facts');
    function fact(label){const row=el('div'),value=el('dd');row.append(el('dt',label),value);facts.append(row);return value;}
    const mode=fact('운전 모드'),localization=fact('위치 추정'),speed=fact('속도');
    const errors=el('section',undefined,'rd-errors');errors.setAttribute('aria-label','로봇 오류');
    const diagnostics=el('details',undefined,'rd-diagnostics');diagnostics.append(el('summary','위치·연결 상세'));
    const diagnosticRows=el('dl',undefined,'rd-diagnostic-rows');diagnostics.append(diagnosticRows);
    function detail(label){const row=el('div'),value=el('dd');row.append(el('dt',label),value);diagnosticRows.append(row);return value;}
    const x=detail('X 좌표'),y=detail('Y 좌표'),heading=detail('방향'),map=detail('배정 맵'),sync=detail('맵 동기화'),version=detail('보고 맵 버전'),score=detail('위치 추정 점수'),size=detail('로봇 크기'),order=detail('오더 ID');
    const updated=el('footer',undefined,'rd-updated');
    root.append(header,status,notice,battery,task,cargo,facts,errors,diagnostics,updated);
    container.replaceChildren(root);
    const card={root,name,connection,status,notice,charge,chargeState,batteryTrack,batteryFill,
      taskTitle,taskStep,progress,mode,localization,speed,errors,diagnostics,cargo,cargoKey:null,
      x,y,heading,map,sync,version,score,size,order,updated,errorKey:null};
    cards.set(container,card);return card;
  }
  function describe(command){
    if(!command)return '마무리 중';
    if(command.type==='wait')return `대기 ${command.duration}초`;
    const type={move:'이동',docking:'충전 도킹',parking:'주차',pickup:'적재',drop:'하차'}[command.type]||command.type;
    return `${type} · ${command.target||'현재 위치'}`;
  }
  function render(container,robot,job){
    const old=cards.get(container),c=old&&container.children[0]===old.root?old:create(container);
    const s=robot.state,b=s?.battery;
    const connected=robot.liveness==='ONLINE';
    const fatal=s?.errors?.some(e=>e.level==='FATAL');
    const tone=s?.state==='EMERGENCY'||fatal||s?.state==='ERROR'?'danger':!s||!connected||s?.paused||s?.mode==='MANUAL'?'warning':s?.state==='BUSY'?'moving':'normal';
    c.root.dataset.tone=tone;
    set(c.name,robot.robot_id);
    set(c.connection,connectionNames[robot.liveness]||robot.liveness||'상태 미수신');
    c.connection.dataset.online=connected?'true':'false';
    set(c.status,!s?'상태 수신 대기':s.state==='EMERGENCY'?'비상정지':s.state==='ERROR'?'오류':s.paused?'일시정지':statusNames[s.sub_state||s.state]||s.state);
    let notice='';
    if(!s)notice='로봇 상태 정보가 없습니다.';
    else if(!robot.registered)notice='현장에 등록되지 않은 로봇입니다.';
    else if(!connected)notice='마지막 수신 상태입니다. 연결을 확인하세요.';
    else if(s.state==='EMERGENCY')notice='로봇의 비상정지 상태를 확인하세요.';
    else if(fatal)notice='오류를 해소해야 작업을 받을 수 있습니다.';
    else if(s.state==='ERROR')notice='로봇의 오류 상태를 확인하세요.';
    else if(s.paused)notice=s.pause_source==='LOCAL'?'로봇에서 일시정지를 해제해야 합니다.':'FMS가 작업을 일시정지했습니다.';
    else if(s.mode==='MANUAL')notice='수동 운전 중입니다.';
    else if(s.localization!=='LOCALIZED')notice='로봇의 위치 확인이 필요합니다.';
    else if(!robot.map_synced)notice='배정 맵 동기화를 기다리고 있습니다.';
    c.notice.hidden=!notice;set(c.notice,notice);
    const charge=Number.isFinite(b?.charge)?Math.min(100,Math.max(0,b.charge)):null;
    set(c.charge,`배터리 ${charge===null?'—':number(charge,0,'%')}`);
    set(c.chargeState,b?.charging?'충전 중':charge!==null&&charge<=20?'잔량 낮음':'');
    c.batteryTrack.dataset.low=charge!==null&&charge<=20?'true':'false';
    c.batteryFill.style.width=`${charge??0}%`;
    if(charge===null)c.batteryTrack.removeAttribute('aria-valuenow');else c.batteryTrack.setAttribute('aria-valuenow',charge);
    const total=job?.commands?.length||0,done=Math.max(0,Math.min(total,job?.done_commands||0));
    set(c.taskTitle,job?.preset_name||job?.job_id||(s?.order_id?'외부 작업':'실행 중인 작업 없음'));
    set(c.taskStep,job?job.status==='assigned'?'로봇 수신 확인 대기':job.status==='canceling'?'작업 취소 확인 중':`${done} / ${total}단계 완료 · ${describe(job.commands?.[done])}`:s?.order_id||'');
    c.taskStep.hidden=!job&&!s?.order_id;
    c.progress.hidden=!total;c.progress.max=total||1;c.progress.value=done;
    set(c.mode,s?.mode==='AUTO'?'자동':s?.mode==='MANUAL'?'수동':'—');
    set(c.localization,({LOCALIZED:'정상',LOCALIZING:'확인 중',UNLOCALIZED:'미확정'})[s?.localization]||'—');
    set(c.speed,number(s?.velocity,2,' m/s'));
    set(c.x,number(s?.pose?.x,3,' m'));set(c.y,number(s?.pose?.y,3,' m'));
    set(c.heading,Number.isFinite(s?.pose?.theta)?number(s.pose.theta*180/Math.PI,1,'°'):'—');
    set(c.map,robot.assigned_map_id||'미배정');set(c.sync,s?robot.map_synced?'일치':'불일치':'확인 전');
    set(c.version,s?.map_version||'—');set(c.score,Number.isFinite(s?.localization_score)?number(s.localization_score*100,0,'%'):'—');
    set(c.size,Number.isFinite(s?.width)&&Number.isFinite(s?.length)?`${number(s.width)} × ${number(s.length)} m`:'—');
    set(c.order,s?.order_id||job?.order_id||'—');
    renderCargo(c,typeof app!=='undefined'?app.cargo?.robots?.[robot.robot_id]:null);
    const errorKey=JSON.stringify(s?.errors||[]);
    if(errorKey!==c.errorKey){
      c.errorKey=errorKey;c.errors.replaceChildren();c.errors.hidden=!s?.errors?.length;
      if(s?.errors?.length){
        c.errors.dataset.severity=fatal?'danger':'warning';
        c.errors.append(el('div',`오류·주의 ${s.errors.length}건`,'rd-label'));
        for(const error of s.errors){
          const item=el('div',undefined,'rd-error');item.dataset.level=error.level;
          item.append(el('strong',`${error.level==='FATAL'?'오류':'주의'} · ${error.error_type}`));
          if(error.description)item.append(el('p',error.description));
          c.errors.append(item);
        }
      }
    }
    if(robot.last_state_at){
      const age=Math.max(0,Math.floor(Date.now()/1000-robot.last_state_at));
      const ago=age<1?'방금':age<60?`${age}초 전`:age<3600?`${Math.floor(age/60)}분 전`:`${Math.floor(age/3600)}시간 전`;
      set(c.updated,`마지막 수신 ${ago} · ${new Date(robot.last_state_at*1000).toLocaleTimeString('ko-KR')}`);
    }else set(c.updated,'상태 수신 이력 없음');
  }
  const trayStates={empty:'비어 있음',reserved:'적재 예정',loading:'적재 중',occupied:'적재됨',unloading:'하차 중',unknown:'확인 필요'};
  function renderCargo(c,cargo){
    const key=JSON.stringify(cargo||null);
    if(key===c.cargoKey)return;
    c.cargoKey=key;c.cargo.replaceChildren();c.cargo.hidden=!cargo;
    if(!cargo)return;
    const bundle=cargo.bundle;
    c.cargo.append(el('div',bundle?`적재 트레이 · ${bundle.summary} ${bundle.progress}`:'적재 트레이','rd-label'));
    const grid=el('div',undefined,'rd-trays');
    for(const tray of cargo.trays){
      const box=el('div',undefined,'rd-tray');box.dataset.state=tray.state;
      box.append(el('strong',tray.tray_id),el('span',trayStates[tray.state]||tray.state));
      if(tray.parcel)box.append(el('small',`${tray.parcel.parcel_id.slice(0,10)}… · ${tray.parcel.household}`));
      grid.append(box);
    }
    c.cargo.append(grid);
    if(cargo.trays.some(t=>t.state==='unknown'))c.cargo.append(el('p','확인 필요한 물품이 있습니다. 보관소 배송 화면에서 실물을 대조하세요.','rd-notice'));
  }
  return {render};
})();
