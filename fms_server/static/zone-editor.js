'use strict';
// 편집 좌표는 world 단위로 보관하고, 그릴 때만 화면 좌표로 변환한다.
// 다른 사용자가 저장한 버전 위에 덮어쓰지 않도록 시작 시 버전을 고정한다.
// 픽업스테이션 모드는 같은 사각형 편집에 픽업 node·반납 node(점+방향)를 더해 한 요청으로 저장한다.
window.zoneEditor = (() => {
  const panel = $('#zoneEditor');
  const form = $('#zoneForm');
  const geometry = window.zoneGeometry;
  const PICKUP_COLOR = '#f0a35e', RETURN_COLOR = '#5ec8f0';
  let active = false, busy = false, zoneId = null, baseVersion = null;
  let rect = null, gesture = null;
  // step: zone | node(픽업) | return(반납). 두 node 는 같은 방식(클릭=위치, 드래그=방향)으로 편집한다.
  let pickup = false, node = null, ret = null, step = 'zone', nodeGesture = null;
  let gates = [];
  const field = name => form.elements.namedItem(name);
  const dimensionKeys = ['x', 'y', 'width', 'height', 'rotation'];
  const nodeKeys = ['nodeX', 'nodeY', 'nodeHeading'];
  const returnKeys = ['returnX', 'returnY', 'returnHeading'];
  const editing = () => step === 'node' ? node : ret;
  const setEditing = value => { if (step === 'node') node = value; else ret = value; };
  const nodeStep = () => pickup && (step === 'node' || step === 'return');
  const minSize = () => view.meta.resolution;
  const bounds = () => ({
    left: view.meta.origin_x, top: view.meta.origin_y,
    right: view.meta.origin_x + view.meta.width_px * view.meta.resolution,
    bottom: view.meta.origin_y + view.meta.height_px * view.meta.resolution,
  });
  const clamp = (n, min, max) => Math.min(max, Math.max(min, n));
  const wrap = theta => Math.atan2(Math.sin(theta), Math.cos(theta));
  function mouse(e) {
    const r = canvas.getBoundingClientRect();
    return {x: e.clientX - r.left, y: e.clientY - r.top};
  }
  function world(p) {
    return {
      x: (p.x - view.tx) / view.scale * view.meta.resolution + view.meta.origin_x,
      y: (p.y - view.ty) / view.scale * view.meta.resolution + view.meta.origin_y,
    };
  }
  function bounded(p) {
    const b = bounds();
    return {x: clamp(p.x, b.left, b.right), y: clamp(p.y, b.top, b.bottom)};
  }
  function inBounds(p) {
    const b = bounds();
    return p.x >= b.left && p.x < b.right && p.y >= b.top && p.y < b.bottom;
  }
  function handles() {
    if (!rect) return [];
    const entries=[['nw',-1,-1],['n',0,-1],['ne',1,-1],['e',1,0],
      ['se',1,1],['s',0,1],['sw',-1,1],['w',-1,0]];
    const result=entries.map(([id,x,y])=>{
      const p=point(geometry.world({x:x*rect.width/2,y:y*rect.height/2},rect));
      return [id,p.x,p.y];
    });
    const p=point(geometry.world({x:0,y:-rect.height/2-30*view.meta.resolution/view.scale},rect));
    result.push(['rotate',p.x,p.y]);
    return result;
  }
  function hint() {
    if (busy) return '맵을 저장하고 있습니다…';
    if (pickup && step === 'node') return node
      ? '클릭해 픽업 지점을 옮기거나, 드래그·숫자 입력으로 로봇 방향을 조절하세요. 구역 밖도 됩니다.'
      : '로봇이 정지해 적재할 위치를 클릭하고, 드래그해 방향을 정하세요.';
    if (pickup && step === 'return') return ret
      ? '클릭해 반납 지점을 옮기거나, 드래그·숫자 입력으로 방향을 조절하세요. 픽업 지점과 1 m 이상 떨어뜨리세요.'
      : '배송을 마친 로봇이 빈 트레이를 내려놓을 위치를 클릭하고, 드래그해 방향을 정하세요.';
    return rect ? '안쪽 드래그: 이동 · 사각 핸들: 크기 · 위쪽 원형 핸들: 회전. 각도도 입력할 수 있습니다.'
      : pickup ? '적재 장치·컨베이어가 있는 영역을 드래그해 사각형으로 그리세요.'
      : '맵에서 드래그해 사각형을 만드세요.';
  }
  function sync() {
    $('#trafficGateFields').hidden = pickup || field('type').value !== 'occupancy';
    if (!pickup) $('#zoneDescription').textContent = field('type').value === 'occupancy'
      ? '한 대씩 통과합니다. 대기·출구는 차체 여유를 두고 배치하세요. 저장하면 새 맵 버전을 로봇에 전달합니다.'
      : '저장하면 새 맵 버전을 활성화하고 로봇에 전달합니다. 엘리베이터 구역은 위치 표시용입니다.';
    $('#addTrafficGate').disabled = busy;
    $('#trafficGateRows').querySelectorAll('input,select,button').forEach(control => { control.disabled = busy; });
    for (const k of dimensionKeys) {
      field(k).disabled = !rect || busy;
      field(k).value = rect ? Number(rect[k].toFixed(9)) : '';
    }
    for (const [keys, value] of [[nodeKeys, node], [returnKeys, ret]]) {
      for (const k of keys) field(k).disabled = !value || busy;
      if (value) {
        field(keys[0]).value = Number(value.x.toFixed(6));
        field(keys[1]).value = Number(value.y.toFixed(6));
        field(keys[2]).value = Number((value.theta * 180 / Math.PI).toFixed(3));
      }
    }
    $('#saveZone').disabled = !rect || busy || (pickup && (!node || !ret));
    $('#cancelZone').disabled = busy;
    // 픽업스테이션 이름은 목적지 이름 변경으로 바꾼다 (작업·프리셋 참조를 함께 갱신).
    field('name').disabled = busy || (pickup && !!zoneId);
    field('type').disabled = busy;
    $('#pickupStepZone').disabled = busy;
    $('#pickupStepNode').disabled = busy || !rect;
    $('#pickupStepReturn').disabled = busy || !rect || !node;
    $('#pickupStepZone').classList.toggle('active', pickup && step === 'zone');
    $('#pickupStepNode').classList.toggle('active', pickup && step === 'node');
    $('#pickupStepReturn').classList.toggle('active', pickup && step === 'return');
    $('#zoneHint').textContent = hint();
  }
  function cancel() {
    active = false; rect = null; gesture = null; node = null; ret = null; nodeGesture = null; pickup = false;
    panel.hidden = true;
    canvas.classList.remove('zone-drawing');
    $('#addZone').classList.remove('active');
    $('#addPickupStation').classList.remove('active');
    draw();
  }
  function canLeave() {
    if (!active) return true;
    if (busy) { notify('구역 저장이 끝날 때까지 기다려 주세요.'); return false; }
    if (!confirm('저장하지 않은 구역 편집을 취소할까요?')) return false;
    cancel();
    return true;
  }
  function start(zone = null, station = null, asPickup = false) {
    if (!view.image) { notify('먼저 활성 맵을 선택하세요.'); return; }
    if (!canLeave()) return;
    if (window.pointEditor && !window.pointEditor.canLeave()) return;
    pickup = asPickup || !!station || zone?.type === 'pickup';
    active = true; zoneId = zone?.zone_id || null; baseVersion = view.version;
    rect = zone ? rectangle(zone.polygon) : null;
    node = station?.node ? {...station.node} : null;
    ret = station?.return_node ? {...station.return_node} : null;
    // 반납 node 도입 전 스테이션은 반납 지점 지정부터 시작한다.
    step = station && node && !ret ? 'return' : 'zone';
    form.reset();
    gates = structuredClone(zone?.gates || []);
    renderGates();
    field('name').value = station?.name || zone?.name || '';
    field('type').value = pickup ? 'prohibit' : (zone?.type || 'prohibit');
    $('#zoneTypeLabel').hidden = pickup;
    $('#pickupNodeFields').hidden = !pickup;
    $('#zoneDescription').textContent = pickup
      ? (station && !station.return_node
        ? '이 스테이션에는 반납 지점이 없어 배송에 쓸 수 없습니다. 반납 지점을 지정해 저장하세요.'
        : '구역은 적재 장치 위치를 알리는 표시용이며 주행을 막지 않습니다. 구역을 바꾸면 새 맵 버전을 로봇에 전달합니다.')
      : '저장하면 새 맵 버전을 활성화하고 로봇에 전달합니다. 엘리베이터 구역은 위치 표시용입니다.';
    $('#saveZone').textContent = pickup ? '픽업스테이션 저장' : '구역 저장';
    $('#zoneEditorTitle').textContent = pickup
      ? (zone ? '픽업스테이션 편집' : '픽업스테이션 추가')
      : (zone ? '구역 편집' : '구역 그리기');
    panel.hidden = false;
    $('#objectInfo').hidden = true;
    $('#layers').open = false;
    view.layers.zones = true;
    $('[data-layer="zones"]').checked = true;
    canvas.classList.add('zone-drawing');
    $(pickup ? '#addPickupStation' : '#addZone').classList.add('active');
    sync(); draw();
    if (!field('name').disabled) field('name').focus();
  }
  function rectangle(polygon) {
    // 벤더의 임의 다각형을 사각형으로 바꾸지 않는다.
    return geometry.fromPolygon(polygon);
  }
  function setStep(next) {
    if (busy || (next !== 'zone' && !rect) || (next === 'return' && !node)) return;
    step = next; sync(); draw();
  }
  function pointerDown(e) {
    if (!active) return false;
    if (busy) return true;
    const p = mouse(e);
    if (nodeStep()) {
      const w = world(p);
      if (!inBounds(w)) { notify('맵 범위 안의 위치를 선택하세요.'); return true; }
      const current = editing();
      nodeGesture = {screen: p, old: current ? {...current} : null};
      setEditing({x: w.x, y: w.y, theta: current?.theta || 0});
      canvas.setPointerCapture(e.pointerId);
      sync(); draw(); return true;
    }
    const w = world(p);
    const handle = handles().find(([,x,y])=>Math.abs(x-p.x)<=8 && Math.abs(y-p.y)<=8);
    if (rect && handle) gesture = {mode: handle[0], start: w, rect: {...rect}};
    else if (rect && Math.abs(geometry.local(w,rect).x)<=rect.width/2 && Math.abs(geometry.local(w,rect).y)<=rect.height/2)
      gesture = {mode:'move', start:w, rect:{...rect}};
    else if (!rect) {
      if (!inBounds(world(p))) return true;
      gesture = {mode:'create', start:bounded(w), rect:null};
    } else return true;
    canvas.setPointerCapture(e.pointerId);
    return true;
  }
  function pointerMove(e) {
    if (!active) return false;
    if (busy) return true;
    if (nodeGesture) {
      const p = mouse(e), dx = p.x - nodeGesture.screen.x, dy = p.y - nodeGesture.screen.y;
      if (Math.hypot(dx, dy) > 6) editing().theta = Math.atan2(dy, dx);
      sync(); draw(); return true;
    }
    if (!gesture) return true;
    const raw = world(mouse(e)), p = bounded(raw), g = gesture, b = bounds(), min = minSize();
    if (g.mode === 'create') {
      rect = {x: Math.min(p.x,g.start.x), y: Math.min(p.y,g.start.y),
        width: Math.abs(p.x-g.start.x), height: Math.abs(p.y-g.start.y), rotation:0};
    } else if (g.mode === 'move') {
      rect = geometry.move(g.rect,raw.x-g.start.x,raw.y-g.start.y,b);
    } else if (g.mode === 'rotate') {
      const c=geometry.center(g.rect);
      const delta=Math.atan2(raw.y-c.y,raw.x-c.x)-Math.atan2(g.start.y-c.y,g.start.x-c.x);
      const candidate={...g.rect,rotation:wrap(g.rect.rotation*Math.PI/180+delta)*180/Math.PI};
      if(geometry.fits(candidate,b))rect=candidate;
    } else {
      const candidate=geometry.resize(g.rect,g.mode,raw,min);
      if(geometry.fits(candidate,b))rect=candidate;
    }
    sync(); draw(); return true;
  }
  function pointerUp(e) {
    if (!active) return false;
    if (nodeGesture) {
      pointerMove(e);
      nodeGesture = null;
      // 픽업 지점을 처음 찍었으면 반납 지점 지정으로 넘어간다.
      if (step === 'node' && !ret) step = 'return';
      if (canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId);
      sync(); draw();
      return true;
    }
    if (gesture) {
      pointerMove(e);
      if (rect && (rect.width < minSize() || rect.height < minSize())) rect=gesture.rect;
      const created = gesture.mode === 'create' && rect;
      gesture=null;
      if(canvas.hasPointerCapture(e.pointerId))canvas.releasePointerCapture(e.pointerId);
      // 새 구역을 그린 직후에는 픽업 지점 지정으로 넘어간다.
      if (pickup && created && !node) step = 'node';
      sync(); draw();
    }
    return true;
  }
  function pointerCancel() {
    if (nodeGesture) { setEditing(nodeGesture.old); nodeGesture = null; sync(); draw(); return; }
    if (!gesture) return;
    rect=gesture.rect; gesture=null; sync(); draw();
  }
  function drawNode(node, color, label, current) {
    if (!node) return;
    const p = point(node);
    if (rect) {
      // 구역과 node 의 연결을 보여준다 (node 는 구역 밖일 수 있다).
      const c = point({x: rect.x + rect.width / 2, y: rect.y + rect.height / 2});
      ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(c.x, c.y); ctx.lineTo(p.x, p.y); ctx.stroke(); ctx.restore();
    }
    ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(node.theta);
    ctx.fillStyle = '#152334'; ctx.strokeStyle = color; ctx.lineWidth = current ? 3 : 2;
    ctx.beginPath(); ctx.arc(0, 0, 9, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    ctx.setLineDash([5, 4]); ctx.beginPath(); ctx.moveTo(12, 0); ctx.lineTo(55, 0); ctx.stroke(); ctx.setLineDash([]);
    ctx.beginPath(); ctx.moveTo(55, 0); ctx.lineTo(45, -6); ctx.lineTo(45, 6); ctx.closePath();
    ctx.fillStyle = color; ctx.fill();
    ctx.restore();
    // 두 node 가 가까워도 읽히도록 반납 표식은 아래쪽에 쓴다.
    text(label, p.x + 13, label.startsWith('⇱') ? p.y + 24 : p.y - 13, color);
  }
  function drawEditor() {
    if (!active || !view.image) return;
    const color = pickup ? PICKUP_COLOR : field('type').value==='occupancy' ? '#59c8be' : field('type').value==='elevator' ? '#ab9aee' : '#f2848c';
    if (rect) {
      const pts=geometry.corners(rect).map(point), a=pts[0];
      ctx.save(); ctx.fillStyle=color+'45'; ctx.strokeStyle=color; ctx.lineWidth=2;
      ctx.beginPath();pts.forEach((p,i)=>i?ctx.lineTo(p.x,p.y):ctx.moveTo(p.x,p.y));ctx.closePath();ctx.fill();
      ctx.setLineDash([6,4]);ctx.stroke();ctx.setLineDash([]);
      if (!pickup || step === 'zone') for (const [id,x,y] of handles()) {
        ctx.fillStyle='#f2f5fb';
        if(id==='rotate'){
          const [,nx,ny]=handles().find(h=>h[0]==='n');
          ctx.beginPath();ctx.moveTo(nx,ny);ctx.lineTo(x,y);ctx.stroke();
          ctx.beginPath();ctx.arc(x,y,6,0,Math.PI*2);ctx.fill();ctx.stroke();
        }else{ctx.fillRect(x-4,y-4,8,8);ctx.strokeRect(x-4,y-4,8,8);}
      }
      text(`${rect.width.toFixed(2)} × ${rect.height.toFixed(2)} m · ${rect.rotation.toFixed(1)}°`, a.x, a.y-12, color);
      ctx.restore();
    }
    if (pickup) {
      drawNode(node, color, '⇲ 픽업', step === 'node');
      drawNode(ret, RETURN_COLOR, '⇱ 반납', step === 'return');
    } else if (field('type').value === 'occupancy') {
      gates.forEach((g,i) => {
        drawNode(g.wait, color, `대기 ${i+1}`, false);
        drawNode(g.exit, '#e2bd79', `출구 ${i+1}`, false);
      });
    }
  }
  async function deleteStation(station) {
    const name = station.name || station.station_id;
    if (!confirm(`픽업스테이션 '${name}'의 구역과 픽업·반납 지점을 함께 삭제할까요?`)) return;
    const url = `${siteURL()}/maps/${enc(station.map_id)}/pickup-stations/${enc(station.station_id)}?base_version=${enc(view.version)}`;
    const result = await request(url, 'DELETE');
    $('#closeInfo').click(); await refreshSite();
    notify(result.map_changed ? '픽업스테이션을 삭제하고 새 맵을 적용했습니다.' : '픽업스테이션을 삭제했습니다.');
  }
  function actions(box, zone) {
    if (zone.type === 'pickup') {
      const station = (app.pickupStations || []).find(s => s.station_id === zone.zone_id);
      // 반납 지점만 없는 스테이션(도입 전)은 편집으로 반납 지점을 추가한다.
      const onlyReturn = station && station.problems?.length === 1 && !station.return_node && station.node;
      if ((station?.status === 'ok' || onlyReturn) && rectangle(zone.polygon))
        box.append(button(onlyReturn ? '반납 지점 추가' : '픽업스테이션 편집', () => start(zone, station)));
      if (station) box.append(button('픽업스테이션 삭제', () => deleteStation(station), 'danger'));
      return;
    }
    if (['prohibit','elevator','occupancy'].includes(zone.type) && rectangle(zone.polygon))
      box.append(button('구역 편집',()=>start(zone)));
    box.append(button('구역 삭제',async()=>{
      if (!confirm(`'${zone.name||zone.zone_id}' 구역을 삭제하고 새 맵 버전을 적용할까요?`)) return;
      const version=view.version, url=`${mapURL()}/zones/${enc(zone.zone_id)}?base_version=${enc(version)}`;
      await request(url,'DELETE');
      $('#closeInfo').click(); await refreshSite(); notify('구역을 삭제하고 새 맵을 적용했습니다.');
    },'danger'));
  }
  for(const k of dimensionKeys) field(k).addEventListener('input',()=>{
    if (!rect || busy) return;
    const values=Object.fromEntries(dimensionKeys.map(n=>[n,Number(field(n).value)]));
    if (dimensionKeys.some(n=>field(n).value==='' || !Number.isFinite(values[n]))) return;
    if (values.width < minSize() || values.height < minSize()) return;
    if(!geometry.fits(values,bounds()))return;
    rect=values; draw();
  });
  for (const [keys, kind] of [[nodeKeys, 'node'], [returnKeys, 'return']]) for (const k of keys)
    field(k).addEventListener('input', () => {
      if (!(kind === 'node' ? node : ret) || busy) return;
      const [x, y, deg] = keys.map(n => Number(field(n).value));
      if (keys.some(n => field(n).value === '') || ![x, y, deg].every(Number.isFinite) || !inBounds({x, y})) return;
      const value = {x, y, theta: wrap((deg % 360) * Math.PI / 180)};
      if (kind === 'node') node = value; else ret = value;
      draw();
    });
  function renderGates() {
    const box = $('#trafficGateRows'); box.replaceChildren();
    const choices = [...app.locations.filter(l => l.map_id === app.map).map(l => ({name:l.name, pose:{x:l.x,y:l.y,theta:l.theta}})),
      ...(view.annotations.locations || []).map(l => ({name:`맵: ${l.name}`,pose:l.pose}))];
    gates.forEach((gate, index) => {
      const row = el('div');
      row.append(el('strong', `방향 ${index+1}`));
      for (const [key, title] of [['wait','대기 node'], ['exit','출구 node']]) {
        const labelNode = el('label', title), select = el('select');
        select.append(new Option('선택하세요', ''));
        if (gate[key]) {
          select.append(new Option(`저장 좌표 (${gate[key].x.toFixed(2)}, ${gate[key].y.toFixed(2)})`, 'saved'));
          select.value = 'saved';
        }
        choices.forEach((c,i) => select.append(new Option(c.name, String(i))));
        select.onchange = () => { gate[key] = select.value === 'saved' ? gate[key]
          : select.value === '' ? null : {...choices[Number(select.value)].pose}; draw(); };
        labelNode.append(select); row.append(labelNode);
      }
      row.append(button('방향 삭제', () => { gates.splice(index,1); renderGates(); draw(); }));
      box.append(row);
    });
  }
  $('#addTrafficGate').onclick = () => {
    gates.push({gate_id:`gate-${Date.now()}-${Math.random().toString(16).slice(2)}`,wait:null,exit:null}); renderGates();
  };
  field('type').addEventListener('change',()=>{ sync(); draw(); });
  $('#pickupStepZone').onclick = () => setStep('zone');
  $('#pickupStepNode').onclick = () => setStep('node');
  $('#pickupStepReturn').onclick = () => setStep('return');
  form.addEventListener('submit',e=>{
    e.preventDefault();
    run(async()=>{
      if (!rect || busy) return;
      const dimensions=Object.fromEntries(dimensionKeys.map(k=>[k,Number(field(k).value)]));
      if(dimensionKeys.some(k=>field(k).value===''||!Number.isFinite(dimensions[k])) || !geometry.fits(dimensions,bounds()))
        throw new Error('회전한 구역의 모든 꼭짓점이 맵 안에 있어야 합니다.');
      const name = field('name').value.trim();
      if (!name) throw new Error(pickup ? '픽업스테이션 이름을 입력하세요.' : '구역 이름을 입력하세요.');
      let url, method, data, done;
      if (pickup) {
        if (!node) throw new Error('픽업 지점을 지정하세요.');
        if (!ret) throw new Error('반납 지점을 지정하세요.');
        data = {base_version: baseVersion, zone: dimensions,
                node: {x: node.x, y: node.y, theta: wrap(node.theta)},
                return_node: {x: ret.x, y: ret.y, theta: wrap(ret.theta)}};
        if (!zoneId) data.name = name;
        url = `${mapURL()}/pickup-stations${zoneId ? '/' + enc(zoneId) : ''}`;
        done = '픽업스테이션을 저장했습니다.';
      } else {
        data={base_version:baseVersion,name,type:field('type').value,...dimensions};
        if (data.type === 'occupancy') {
          if (!gates.length || gates.some(g => !g.wait || !g.exit)) throw new Error('방향별 대기 node와 출구 node를 선택하세요.');
          data.gates = gates;
        }
        url=`${mapURL()}/zones${zoneId?'/'+enc(zoneId):''}`;
        done = '구역을 저장하고 새 맵을 적용했습니다.';
      }
      method = zoneId ? 'PUT' : 'POST';
      const camera={tx:view.tx,ty:view.ty,scale:view.scale};
      busy=true; sync();
      try {
        const result = await request(url,method,data);
        cancel(); await refreshSite(); Object.assign(view,camera); draw();
        notify(done);
        if (result.warnings?.length) {
          console.info('맵 편집 경고', result.warnings);
          const gap = result.warnings.find(w => w.includes('간격'));
          if (gap) notify(`${done} 주의: ${gap}`);
        }
      } finally {busy=false; if(active)sync();}
    });
  });
  $('#addZone').onclick=()=>{ $('#addObjectMenu').open=false; start(); };
  $('#addPickupStation').onclick=()=>{ $('#addObjectMenu').open=false; start(null, null, true); };
  $('#cancelZone').onclick=()=>{if(canLeave())run(()=>refreshSite());};
  window.addEventListener('beforeunload',e=>{if(active){e.preventDefault();e.returnValue='';}});
  return {get active(){return active;},get editingId(){return active?zoneId:null;},cancel,canLeave,draw:drawEditor,actions,deleteStation,pointerDown,pointerMove,pointerUp,pointerCancel};
})();
