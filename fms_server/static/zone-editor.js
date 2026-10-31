'use strict';
// 편집 좌표는 world 단위로 보관하고, 그릴 때만 화면 좌표로 변환한다.
// 다른 사용자가 저장한 버전 위에 덮어쓰지 않도록 시작 시 버전을 고정한다.
// 픽업스테이션 모드는 같은 사각형 편집에 픽업 node(점+방향)를 더해 한 요청으로 저장한다.
window.zoneEditor = (() => {
  const panel = $('#zoneEditor');
  const form = $('#zoneForm');
  const PICKUP_COLOR = '#f0a35e';
  let active = false, busy = false, zoneId = null, baseVersion = null;
  let rect = null, gesture = null;
  let pickup = false, node = null, step = 'zone', nodeGesture = null;
  const field = name => form.elements.namedItem(name);
  const dimensionKeys = ['x', 'y', 'width', 'height'];
  const nodeKeys = ['nodeX', 'nodeY', 'nodeHeading'];
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
    const a = point(rect), b = point({x: rect.x + rect.width, y: rect.y + rect.height});
    return [
      ['nw', a.x, a.y], ['n', (a.x+b.x)/2, a.y], ['ne', b.x, a.y],
      ['e', b.x, (a.y+b.y)/2], ['se', b.x, b.y], ['s', (a.x+b.x)/2, b.y],
      ['sw', a.x, b.y], ['w', a.x, (a.y+b.y)/2],
    ];
  }
  function hint() {
    if (busy) return '맵을 저장하고 있습니다…';
    if (pickup && step === 'node') return node
      ? '클릭해 픽업 지점을 옮기거나, 드래그·숫자 입력으로 로봇 방향을 조절하세요. 구역 밖도 됩니다.'
      : '로봇이 정지해 적재할 위치를 클릭하고, 드래그해 방향을 정하세요.';
    return rect ? '안쪽을 드래그해 이동하고, 핸들을 드래그해 크기를 조절하세요.'
      : pickup ? '적재 장치·컨베이어가 있는 영역을 드래그해 사각형으로 그리세요.'
      : '맵에서 드래그해 사각형을 만드세요.';
  }
  function sync() {
    for (const k of dimensionKeys) {
      field(k).disabled = !rect || busy;
      field(k).value = rect ? Number(rect[k].toFixed(6)) : '';
    }
    for (const k of nodeKeys) field(k).disabled = !node || busy;
    if (node) {
      field('nodeX').value = Number(node.x.toFixed(6));
      field('nodeY').value = Number(node.y.toFixed(6));
      field('nodeHeading').value = Number((node.theta * 180 / Math.PI).toFixed(3));
    }
    $('#saveZone').disabled = !rect || busy || (pickup && !node);
    $('#cancelZone').disabled = busy;
    // 픽업스테이션 이름은 목적지 이름 변경으로 바꾼다 (작업·프리셋 참조를 함께 갱신).
    field('name').disabled = busy || (pickup && !!zoneId);
    field('type').disabled = busy;
    $('#pickupStepZone').disabled = busy;
    $('#pickupStepNode').disabled = busy || !rect;
    $('#pickupStepZone').classList.toggle('active', pickup && step === 'zone');
    $('#pickupStepNode').classList.toggle('active', pickup && step === 'node');
    $('#zoneHint').textContent = hint();
  }
  function cancel() {
    active = false; rect = null; gesture = null; node = null; nodeGesture = null; pickup = false;
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
    step = 'zone';
    form.reset();
    field('name').value = station?.name || zone?.name || '';
    field('type').value = pickup ? 'prohibit' : (zone?.type || 'prohibit');
    $('#zoneTypeLabel').hidden = pickup;
    $('#pickupNodeFields').hidden = !pickup;
    $('#zoneDescription').textContent = pickup
      ? '구역은 적재 장치 위치를 알리는 표시용이며 주행을 막지 않습니다. 저장하면 새 맵 버전을 로봇에 전달합니다.'
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
    const pts = polygon.length === 5 ? polygon.slice(0,4) : polygon;
    if (pts.length !== 4) return null;
    const xs = [...new Set(pts.map(p=>p.x))], ys = [...new Set(pts.map(p=>p.y))];
    if (xs.length !== 2 || ys.length !== 2 || new Set(pts.map(p=>`${p.x},${p.y}`)).size !== 4) return null;
    return {x: Math.min(...xs), y: Math.min(...ys), width: Math.abs(xs[1]-xs[0]), height: Math.abs(ys[1]-ys[0])};
  }
  function setStep(next) {
    if (busy || (next === 'node' && !rect)) return;
    step = next; sync(); draw();
  }
  function pointerDown(e) {
    if (!active) return false;
    if (busy) return true;
    const p = mouse(e);
    if (pickup && step === 'node') {
      const w = world(p);
      if (!inBounds(w)) { notify('맵 범위 안의 위치를 선택하세요.'); return true; }
      nodeGesture = {screen: p, old: node ? {...node} : null};
      node = {x: w.x, y: w.y, theta: node?.theta || 0};
      canvas.setPointerCapture(e.pointerId);
      sync(); draw(); return true;
    }
    const w = bounded(world(p));
    const handle = handles().find(([,x,y])=>Math.abs(x-p.x)<=8 && Math.abs(y-p.y)<=8);
    if (rect && handle) gesture = {mode: handle[0], start: w, rect: {...rect}};
    else if (rect && w.x>=rect.x && w.x<=rect.x+rect.width && w.y>=rect.y && w.y<=rect.y+rect.height)
      gesture = {mode:'move', start:w, rect:{...rect}};
    else if (!rect) {
      if (!inBounds(world(p))) return true;
      gesture = {mode:'create', start:w, rect:null};
    } else return true;
    canvas.setPointerCapture(e.pointerId);
    return true;
  }
  function pointerMove(e) {
    if (!active) return false;
    if (busy) return true;
    if (nodeGesture) {
      const p = mouse(e), dx = p.x - nodeGesture.screen.x, dy = p.y - nodeGesture.screen.y;
      if (Math.hypot(dx, dy) > 6) node.theta = Math.atan2(dy, dx);
      sync(); draw(); return true;
    }
    if (!gesture) return true;
    const p = bounded(world(mouse(e))), g = gesture, b = bounds(), min = minSize();
    if (g.mode === 'create') {
      rect = {x: Math.min(p.x,g.start.x), y: Math.min(p.y,g.start.y),
        width: Math.abs(p.x-g.start.x), height: Math.abs(p.y-g.start.y)};
    } else if (g.mode === 'move') {
      rect = {...g.rect, x: clamp(g.rect.x+p.x-g.start.x,b.left,b.right-g.rect.width),
        y: clamp(g.rect.y+p.y-g.start.y,b.top,b.bottom-g.rect.height)};
    } else {
      let left=g.rect.x, top=g.rect.y, right=left+g.rect.width, bottom=top+g.rect.height;
      if (g.mode.includes('w')) left=Math.min(p.x,right-min);
      if (g.mode.includes('e')) right=Math.max(p.x,left+min);
      if (g.mode.includes('n')) top=Math.min(p.y,bottom-min);
      if (g.mode.includes('s')) bottom=Math.max(p.y,top+min);
      rect={x:left,y:top,width:right-left,height:bottom-top};
    }
    sync(); draw(); return true;
  }
  function pointerUp(e) {
    if (!active) return false;
    if (nodeGesture) {
      pointerMove(e);
      nodeGesture = null;
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
    if (nodeGesture) { node = nodeGesture.old; nodeGesture = null; sync(); draw(); return; }
    if (!gesture) return;
    rect=gesture.rect; gesture=null; sync(); draw();
  }
  function drawNode(color) {
    if (!node) return;
    const p = point(node);
    if (rect) {
      // 구역과 node 의 연결을 보여준다 (node 는 구역 밖일 수 있다).
      const c = point({x: rect.x + rect.width / 2, y: rect.y + rect.height / 2});
      ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(c.x, c.y); ctx.lineTo(p.x, p.y); ctx.stroke(); ctx.restore();
    }
    ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(node.theta);
    ctx.fillStyle = '#152334'; ctx.strokeStyle = color; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(0, 0, 9, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    ctx.setLineDash([5, 4]); ctx.beginPath(); ctx.moveTo(12, 0); ctx.lineTo(55, 0); ctx.stroke(); ctx.setLineDash([]);
    ctx.beginPath(); ctx.moveTo(55, 0); ctx.lineTo(45, -6); ctx.lineTo(45, 6); ctx.closePath();
    ctx.fillStyle = color; ctx.fill();
    ctx.restore();
    text('픽업 지점', p.x + 13, p.y - 13, color);
  }
  function drawEditor() {
    if (!active || !view.image) return;
    const color = pickup ? PICKUP_COLOR : field('type').value==='elevator' ? '#ab9aee' : '#f2848c';
    if (rect) {
      const a=point(rect), b=point({x:rect.x+rect.width,y:rect.y+rect.height});
      ctx.save(); ctx.fillStyle=color+'45'; ctx.strokeStyle=color; ctx.lineWidth=2;
      ctx.fillRect(a.x,a.y,b.x-a.x,b.y-a.y);
      ctx.setLineDash([6,4]); ctx.strokeRect(a.x,a.y,b.x-a.x,b.y-a.y); ctx.setLineDash([]);
      if (!pickup || step === 'zone') for (const [,x,y] of handles()) {
        ctx.fillStyle='#f2f5fb'; ctx.fillRect(x-4,y-4,8,8); ctx.strokeRect(x-4,y-4,8,8);
      }
      text(`${rect.width.toFixed(2)} × ${rect.height.toFixed(2)} m`, a.x, a.y-12, color);
      ctx.restore();
    }
    if (pickup) drawNode(color);
  }
  async function deleteStation(station) {
    const name = station.name || station.station_id;
    if (!confirm(`픽업스테이션 '${name}'의 구역과 픽업 지점을 함께 삭제할까요?`)) return;
    const url = `${siteURL()}/maps/${enc(station.map_id)}/pickup-stations/${enc(station.station_id)}?base_version=${enc(view.version)}`;
    const result = await request(url, 'DELETE');
    $('#closeInfo').click(); await refreshSite();
    notify(result.map_changed ? '픽업스테이션을 삭제하고 새 맵을 적용했습니다.' : '픽업스테이션을 삭제했습니다.');
  }
  function actions(box, zone) {
    if (zone.type === 'pickup') {
      const station = (app.pickupStations || []).find(s => s.station_id === zone.zone_id);
      if (station?.status === 'ok' && rectangle(zone.polygon))
        box.append(button('픽업스테이션 편집', () => start(zone, station)));
      if (station) box.append(button('픽업스테이션 삭제', () => deleteStation(station), 'danger'));
      return;
    }
    if (['prohibit','elevator'].includes(zone.type) && rectangle(zone.polygon))
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
    const b=bounds();
    if(values.x<b.left || values.y<b.top || values.x+values.width>b.right || values.y+values.height>b.bottom)return;
    rect=values; draw();
  });
  for (const k of nodeKeys) field(k).addEventListener('input', () => {
    if (!node || busy) return;
    const x = Number(field('nodeX').value), y = Number(field('nodeY').value), deg = Number(field('nodeHeading').value);
    if (nodeKeys.some(n => field(n).value === '') || ![x, y, deg].every(Number.isFinite) || !inBounds({x, y})) return;
    node = {x, y, theta: wrap((deg % 360) * Math.PI / 180)}; draw();
  });
  field('type').addEventListener('change',()=>draw());
  $('#pickupStepZone').onclick = () => setStep('zone');
  $('#pickupStepNode').onclick = () => setStep('node');
  form.addEventListener('submit',e=>{
    e.preventDefault();
    run(async()=>{
      if (!rect || busy) return;
      const dimensions=Object.fromEntries(dimensionKeys.map(k=>[k,Number(field(k).value)]));
      const name = field('name').value.trim();
      if (!name) throw new Error(pickup ? '픽업스테이션 이름을 입력하세요.' : '구역 이름을 입력하세요.');
      let url, method, data, done;
      if (pickup) {
        if (!node) throw new Error('픽업 지점을 지정하세요.');
        data = {base_version: baseVersion, zone: dimensions, node: {x: node.x, y: node.y, theta: wrap(node.theta)}};
        if (!zoneId) data.name = name;
        url = `${mapURL()}/pickup-stations${zoneId ? '/' + enc(zoneId) : ''}`;
        done = '픽업스테이션을 저장했습니다.';
      } else {
        data={base_version:baseVersion,name,type:field('type').value,...dimensions};
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
        if (result.warnings?.length) console.info('맵 편집 경고', result.warnings);
      } finally {busy=false; if(active)sync();}
    });
  });
  $('#addZone').onclick=()=>{ $('#addObjectMenu').open=false; start(); };
  $('#addPickupStation').onclick=()=>{ $('#addObjectMenu').open=false; start(null, null, true); };
  $('#cancelZone').onclick=()=>{if(canLeave())run(()=>refreshSite());};
  window.addEventListener('beforeunload',e=>{if(active){e.preventDefault();e.returnValue='';}});
  return {get active(){return active;},get editingId(){return active?zoneId:null;},cancel,canLeave,draw:drawEditor,actions,deleteStation,pointerDown,pointerMove,pointerUp,pointerCancel};
})();
