'use strict';
// 목적지와 물리 충전기는 별도 데이터다. 도킹 스테이션은 서버에서 둘을 함께 등록한다.
window.pointEditor = (()=>{
  const form=$('#mapPointForm'), panel=$('#mapPointEditor');
  const input=name=>form.elements.namedItem(name);
  let active=false,busy=false,kind='location',pose=null,gesture=null,baseVersion=null;
  function screen(e){const r=canvas.getBoundingClientRect();return {x:e.clientX-r.left,y:e.clientY-r.top};}
  function world(p){return {x:(p.x-view.tx)/view.scale*view.meta.resolution+view.meta.origin_x,
    y:(p.y-view.ty)/view.scale*view.meta.resolution+view.meta.origin_y};}
  function inBounds(p){const m=view.meta;return p.x>=m.origin_x&&p.y>=m.origin_y&&p.x<m.origin_x+m.width_px*m.resolution&&p.y<m.origin_y+m.height_px*m.resolution;}
  function sync(){
    for(const key of ['x','y','heading'])input(key).disabled=!pose||busy;
    if(pose){input('x').value=Number(pose.x.toFixed(6));input('y').value=Number(pose.y.toFixed(6));input('heading').value=Number((pose.theta*180/Math.PI).toFixed(3));}
    $('#saveMapPoint').disabled=!pose||busy;$('#cancelMapPoint').disabled=busy;
    input('name').disabled=busy;input('type').disabled=busy||kind==='dock';input('entry').disabled=busy||kind!=='dock';
    $('#mapPointHint').textContent=busy?'위치를 저장하고 있습니다…':pose?'클릭해 위치를 옮기거나, 드래그·숫자 입력으로 방향을 조절하세요.':'맵에서 위치를 클릭하거나 드래그해 방향을 정하세요.';
  }
  function cancel(){active=false;pose=null;gesture=null;panel.hidden=true;canvas.classList.remove('point-placing');draw();}
  function canLeave(){
    if(!active)return true;
    if(busy){notify('위치 저장이 끝날 때까지 기다려 주세요.');return false;}
    if(!confirm('저장하지 않은 위치 추가를 취소할까요?'))return false;
    cancel();return true;
  }
  function start(nextKind){
    $('#addObjectMenu').open=false;
    if(!view.image){notify('먼저 활성 맵을 선택하세요.');return;}
    if(!canLeave() || (window.zoneEditor && !window.zoneEditor.canLeave()))return;
    kind=nextKind;active=true;baseVersion=view.version;form.reset();pose=null;
    input('name').placeholder=kind==='dock'?'충전소-A':'작업 지점-A';
    $('#mapPointTitle').textContent=kind==='dock'?'도킹 스테이션 추가':'목적지 추가';
    $('#mapPointTypeLabel').hidden=kind==='dock';$('#mapPointEntryLabel').hidden=kind!=='dock';
    $('#mapPointDescription').textContent=kind==='dock'?'충전 도킹 위치와 작업 목적지를 함께 등록합니다. 저장하면 로봇에 새 맵을 전달합니다.':'저장하면 현재 맵의 작업 목적지로 사용할 수 있습니다.';
    $('#objectInfo').hidden=true;$('#layers').open=false;panel.hidden=false;
    canvas.classList.add('point-placing');sync();input('name').focus();
  }
  function pointerDown(e){
    if(!active)return false;if(busy)return true;
    const p=screen(e),w=world(p);if(!inBounds(w)){notify('맵 범위 안의 위치를 선택하세요.');return true;}
    gesture={screen:p,old:pose?{...pose}:null};pose={...w,theta:pose?.theta||0};
    canvas.setPointerCapture(e.pointerId);sync();draw();return true;
  }
  function pointerMove(e){
    if(!active)return false;if(!gesture||busy)return true;
    const p=screen(e),dx=p.x-gesture.screen.x,dy=p.y-gesture.screen.y;
    if(Math.hypot(dx,dy)>6)pose.theta=Math.atan2(dy,dx);
    sync();draw();return true;
  }
  function pointerUp(e){
    if(!active)return false;
    if(gesture){pointerMove(e);gesture=null;if(canvas.hasPointerCapture(e.pointerId))canvas.releasePointerCapture(e.pointerId);}
    return true;
  }
  function pointerCancel(){if(gesture){pose=gesture.old;gesture=null;sync();draw();}}
  function drawEditor(){
    if(!active||!pose||!view.image)return;
    const p=point(pose),color=kind==='dock'?'#77d8c3':'#e2bd79';
    ctx.save();ctx.translate(p.x,p.y);ctx.rotate(pose.theta);ctx.fillStyle='#152334';ctx.strokeStyle=color;ctx.lineWidth=2;
    ctx.beginPath();if(kind==='dock')ctx.rect(-10,-10,20,20);else ctx.arc(0,0,9,0,Math.PI*2);ctx.fill();ctx.stroke();
    ctx.setLineDash([5,4]);ctx.beginPath();ctx.moveTo(12,0);ctx.lineTo(65,0);ctx.stroke();ctx.setLineDash([]);
    ctx.beginPath();ctx.moveTo(65,0);ctx.lineTo(55,-6);ctx.lineTo(55,6);ctx.closePath();ctx.fillStyle=color;ctx.fill();
    ctx.restore();text(input('name').value.trim()||'새 위치',p.x+15,p.y-17,color);
  }
  for(const key of ['x','y','heading'])input(key).addEventListener('input',()=>{
    if(!pose||busy)return;
    const x=Number(input('x').value),y=Number(input('y').value),degrees=Number(input('heading').value);
    if(['x','y','heading'].some(k=>input(k).value==='')||![x,y,degrees].every(Number.isFinite)||!inBounds({x,y}))return;
    const theta=(degrees%360)*Math.PI/180;
    pose={x,y,theta:Math.atan2(Math.sin(theta),Math.cos(theta))};draw();
  });
  input('name').addEventListener('input',()=>draw());
  form.addEventListener('submit',e=>{
    e.preventDefault();run(async()=>{
      if(!pose||busy)return;
      const name=input('name').value.trim(),x=Number(input('x').value),y=Number(input('y').value),heading=Number(input('heading').value);
      if(!name)throw new Error('이름을 입력하세요.');
      if(![x,y,heading].every(Number.isFinite)||!inBounds({x,y}))throw new Error('맵 범위 안의 좌표와 유효한 방향을 입력하세요.');
      const radians=(heading%360)*Math.PI/180;
      const data={base_version:baseVersion,name,x,y,theta:Math.atan2(Math.sin(radians),Math.cos(radians))};
      if(kind==='dock')data.entry=input('entry').value;else data.type=input('type').value;
      const url=`${mapURL()}/${kind==='dock'?'docking-stations':'locations'}`,camera={tx:view.tx,ty:view.ty,scale:view.scale};
      busy=true;sync();try{
        const result=await request(url,'POST',data);
        const layer=kind==='dock'||data.type==='parking'?'docks':'locations';
        view.layers[layer]=true;$(`[data-layer="${layer}"]`).checked=true;
        cancel();await refreshSite();Object.assign(view,camera);draw();
        notify(`${kind==='dock'?'도킹 스테이션을':'목적지를'} 추가했습니다.${result.warnings?.length?' '+result.warnings.join(' / '):''}`);
      }finally{busy=false;if(active)sync();}
    });
  });
  $('#addLocation').onclick=()=>start('location');$('#addDockingStation').onclick=()=>start('dock');
  $('#addObjectMenu').addEventListener('toggle',()=>{if($('#addObjectMenu').open)$('#layers').open=false;});
  $('#cancelMapPoint').onclick=()=>{if(canLeave())run(()=>refreshSite());};
  $('#addObjectMenu').addEventListener('keydown',e=>{if(e.key==='Escape'){$('#addObjectMenu').open=false;$('#mapAddButton').focus();}});
  document.addEventListener('pointerdown',e=>{if(!$('#addObjectMenu').contains(e.target))$('#addObjectMenu').open=false;});
  window.addEventListener('beforeunload',e=>{if(active){e.preventDefault();e.returnValue='';}});
  return {get active(){return active;},canLeave,cancel,draw:drawEditor,pointerDown,pointerMove,pointerUp,pointerCancel};
})();
