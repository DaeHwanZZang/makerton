'use strict';
// 프리셋 초안은 주기 갱신과 분리한다. 게시판에는 저장된 프리셋의 복사본을 올린다.
window.jobUI = (()=>{
  let presets=app.presets||[], steps=[], editId=null, stepIndex=null, dirty=false, filter='pending', saving=false;
  const $f=id=>$('#'+id);
  const kinds={move:'이동',wait:'대기',docking:'충전 도킹',parking:'주차',pickup:'적재',drop:'하차'};
  function describe(c){
    if(c.type==='wait')return `대기 ${c.duration}초`;
    return `${kinds[c.type]||c.type} ${c.target||''}`;
  }
  function preview(){
    const p=presets.find(p=>p.preset_id===$f('publishPreset').value);
    $f('publishPreview').textContent=p?p.commands.map(describe).join(' → '):'프리셋을 먼저 등록하세요.';
  }
  function render(){
    options($f('stepTarget'),app.locations.map(l=>[l.name,`${l.name} (${l.map_id})`]),'목적지 선택');
    options($f('publishPreset'),presets.map(p=>[p.preset_id,p.name]),'프리셋 선택');
    options($f('publishRobot'),(app.detail?.robots||[]).map(r=>[r.robot_id,r.robot_id]),'로봇 선택');
    const tb=$f('presetRows');tb.replaceChildren();
    for(const p of presets)row(tb,[p.name,p.commands.map(describe).join(' → '),new Date(p.updated_at).toLocaleString(),[
      button('편집',()=>{if(!canLeave())return;editId=p.preset_id;steps=structuredClone(p.commands);$f('presetName').value=p.name;$f('presetTitle').textContent='프리셋 편집';dirty=false;renderSteps();showPage('presets');}),
      button('게시판으로',()=>{showPage('jobs');$f('publishPreset').value=p.preset_id;preview();}),
      button('삭제',async()=>{if(!confirm(`'${p.name}' 프리셋을 삭제할까요? 이미 생성된 작업은 유지됩니다.`))return;await request(`${siteURL()}/presets/${enc(p.preset_id)}`,'DELETE');await refreshSite();},'danger')
    ]]);
    empty(tb,4,app.site?'등록한 프리셋이 없습니다. 단계를 추가해 첫 프리셋을 만드세요.':'먼저 현장을 선택하세요.');
    for(const id of ['savePreset','saveStep'])$f(id).disabled=!app.site||saving;
    $f('publishForm').querySelector('button').disabled=!app.site||!presets.length||saving;
    preview();
  }
  function reset(){editId=null;steps=[];stepIndex=null;dirty=false;$f('presetName').value='';$f('presetTitle').textContent='프리셋 만들기';resetStep();renderSteps();}
  function resetStep(){stepIndex=null;$f('saveStep').textContent='단계 추가';$f('cancelStep').hidden=true;}
  function canLeave(){
    if(saving){notify('저장이 끝날 때까지 기다려 주세요.');return false;}
    if(dirty&&!confirm('저장하지 않은 프리셋 변경을 취소할까요?'))return false;
    if(dirty)reset();return true;
  }
  function typeChanged(){
    const type=$f('stepType').value,dock=['docking','parking'].includes(type);
    $f('stepTargetLabel').hidden=type==='wait';$f('stepDurationLabel').hidden=type!=='wait';
    $f('stepTarget').disabled=type==='wait';$f('stepDuration').disabled=type!=='wait';
    for(const n of document.querySelectorAll('.dock-field')){
      n.hidden=!dock;
      n.querySelector('input,select').disabled=!dock;
    }
    $f('stepChargeLabel').hidden=type!=='docking';
    $f('stepCharge').disabled=type!=='docking';
    $f('stepHelp').textContent=type==='wait'?'직전 단계의 목적지에서 기다립니다. 첫 단계로는 사용할 수 없습니다.':type==='docking'?'맵에 chargers.json으로 등록된 충전기 위치를 선택하세요. 목표 배터리 기준은 다음 작업의 수락 조건입니다.':'목적지는 모두 같은 맵이어야 합니다.';
  }
  function renderSteps(){
    const list=$f('presetSteps');list.replaceChildren();
    steps.forEach((c,i)=>{
      const li=el('li');li.append(el('span',`${i+1}. ${describe(c)}`));const actions=el('div');
      const up=button('↑',()=>{[steps[i-1],steps[i]]=[steps[i],steps[i-1]];dirty=true;resetStep();renderSteps();});up.disabled=i===0;up.setAttribute('aria-label',`${i+1}단계 위로`);
      const down=button('↓',()=>{[steps[i+1],steps[i]]=[steps[i],steps[i+1]];dirty=true;resetStep();renderSteps();});down.disabled=i===steps.length-1;down.setAttribute('aria-label',`${i+1}단계 아래로`);
      actions.append(up,down,button('수정',()=>{
        stepIndex=i;$f('stepType').value=c.type;$f('stepTarget').value=c.target||'';$f('stepDuration').value=c.duration||10;
        $f('stepEntry').value=c.params?.entry||'FORWARD';$f('stepDistance').value=c.params?.undock_distance||.6;
        $f('stepSpeed').value=c.params?.speed||.2;$f('stepCharge').value=c.params?.min_charge??80;
        typeChanged();$f('saveStep').textContent='단계 수정';$f('cancelStep').hidden=false;
      }),button('삭제',()=>{steps.splice(i,1);dirty=true;resetStep();renderSteps();},'danger'));
      li.append(actions);list.append(li);
    });
    if(!steps.length)list.append(el('li','아직 단계가 없습니다. 이동 단계부터 추가하세요.','muted'));
  }
  $f('stepForm').addEventListener('submit',e=>{
    e.preventDefault();run(()=>{
      const type=$f('stepType').value,c={type};
      if(type==='wait'){
        c.duration=Number($f('stepDuration').value);if(!(c.duration>0))throw new Error('대기 시간을 입력하세요.');
        if((stepIndex??steps.length)===0)throw new Error('대기 전에 이동 단계를 추가하세요.');
      }else{
        c.target=$f('stepTarget').value;if(!c.target)throw new Error('목적지를 선택하세요.');
        if(['docking','parking'].includes(type)){
          c.params={entry:$f('stepEntry').value,undock_distance:Number($f('stepDistance').value),speed:Number($f('stepSpeed').value)};
          if(!(c.params.undock_distance>0)||!(c.params.speed>0))throw new Error('진입 거리와 속도는 0보다 커야 합니다.');
          if(type==='docking')c.params.min_charge=Number($f('stepCharge').value);
        }
      }
      if(stepIndex===null)steps.push(c);else steps[stepIndex]=c;
      dirty=true;resetStep();renderSteps();
    });
  });
  $f('savePreset').onclick=()=>run(async()=>{
    if(!app.site||saving)return;const name=$f('presetName').value.trim();if(!name||!steps.length)throw new Error('프리셋 이름과 단계를 입력하세요.');
    const payload={name,commands:structuredClone(steps)},url=`${siteURL()}/presets${editId?'/'+enc(editId):''}`;
    saving=true;render();try{const result=await request(url,editId?'PUT':'POST',payload);editId=result.preset_id;dirty=false;await refreshSite();notify('프리셋을 저장했습니다.');}finally{saving=false;render();}
  });
  $f('publishForm').addEventListener('submit',e=>{
    e.preventDefault();run(async()=>{
      if(saving)return;const preset=$f('publishPreset').value;if(!preset)throw new Error('프리셋을 선택하세요.');
      const direct=$f('publishMode').value==='direct',robot=$f('publishRobot').value;
      if(direct&&!robot)throw new Error('실행할 로봇을 선택하세요.');
      saving=true;render();try{
        await request(`${siteURL()}/presets/${enc(preset)}/publish`,'POST',direct?{robot_id:robot}:{});
        filter=direct?'active':'pending';updateFilter();await refreshSite();notify(direct?'로봇에 작업을 배정했습니다.':'게시판에 등록했습니다. 가용 로봇을 기다립니다.');
      }finally{saving=false;render();}
    });
  });
  function updateFilter(){for(const b of document.querySelectorAll('[data-job-filter]'))b.classList.toggle('active',b.dataset.jobFilter===filter);renderJobs();}
  for(const b of document.querySelectorAll('[data-job-filter]'))b.onclick=()=>{filter=b.dataset.jobFilter;updateFilter();};
  $f('publishMode').onchange=()=>{$f('publishRobotLabel').hidden=$f('publishMode').value!=='direct';};
  $f('publishPreset').onchange=preview;$f('stepType').onchange=typeChanged;
  $f('presetName').oninput=()=>dirty=true;$f('cancelStep').onclick=resetStep;
  $f('resetPreset').onclick=()=>{if(canLeave())reset();};
  window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
  typeChanged();renderSteps();render();
  return {render,describe,canLeave,setPresets(data){presets=data;},resetSite(){presets=[];reset();},
    visible(j){return filter==='pending'?j.status==='pending':filter==='active'?['assigned','working','canceling'].includes(j.status):['success','failed','canceled'].includes(j.status);}};
})();
