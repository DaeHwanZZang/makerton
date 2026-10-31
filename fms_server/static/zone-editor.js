'use strict';
// 편집 좌표는 world 단위로 보관하고, 그릴 때만 화면 좌표로 변환한다.
// 다른 사용자가 저장한 버전 위에 덮어쓰지 않도록 시작 시 버전을 고정한다.
window.zoneEditor = (() => {
  const panel = $('#zoneEditor');
  const form = $('#zoneForm');
  let active = false, busy = false, zoneId = null, baseVersion = null;
  let rect = null, gesture = null;
  const field = name => form.elements.namedItem(name);
  const dimensionKeys = ['x', 'y', 'width', 'height'];
  const minSize = () => view.meta.resolution;
  const bounds = () => ({
    left: view.meta.origin_x, top: view.meta.origin_y,
    right: view.meta.origin_x + view.meta.width_px * view.meta.resolution,
    bottom: view.meta.origin_y + view.meta.height_px * view.meta.resolution,
  });
  const clamp = (n, min, max) => Math.min(max, Math.max(min, n));
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
  function handles() {
    if (!rect) return [];
    const a = point(rect), b = point({x: rect.x + rect.width, y: rect.y + rect.height});
    return [
      ['nw', a.x, a.y], ['n', (a.x+b.x)/2, a.y], ['ne', b.x, a.y],
      ['e', b.x, (a.y+b.y)/2], ['se', b.x, b.y], ['s', (a.x+b.x)/2, b.y],
      ['sw', a.x, b.y], ['w', a.x, (a.y+b.y)/2],
    ];
  }
  function sync() {
    for (const k of dimensionKeys) {
      field(k).disabled = !rect || busy;
      field(k).value = rect ? Number(rect[k].toFixed(6)) : '';
    }
    $('#saveZone').disabled = !rect || busy;
    $('#cancelZone').disabled = busy;
    field('name').disabled = busy;
    field('type').disabled = busy;
    $('#zoneHint').textContent = busy ? '맵을 저장하고 있습니다…' : rect
      ? '안쪽을 드래그해 이동하고, 핸들을 드래그해 크기를 조절하세요.'
      : '맵에서 드래그해 사각형을 만드세요.';
  }
  function cancel() {
    active = false; rect = null; gesture = null;
    panel.hidden = true;
    canvas.classList.remove('zone-drawing');
    $('#addZone').classList.remove('active');
    draw();
  }
  function canLeave() {
    if (!active) return true;
    if (busy) { notify('구역 저장이 끝날 때까지 기다려 주세요.'); return false; }
    if (!confirm('저장하지 않은 구역 편집을 취소할까요?')) return false;
    cancel();
    return true;
  }
  function start(zone = null) {
    if (!view.image) { notify('먼저 활성 맵을 선택하세요.'); return; }
    if (!canLeave()) return;
    if (window.pointEditor && !window.pointEditor.canLeave()) return;
    active = true; zoneId = zone?.zone_id || null; baseVersion = view.version;
    rect = zone ? rectangle(zone.polygon) : null;
    form.reset();
    field('name').value = zone?.name || '';
    field('type').value = zone?.type || 'prohibit';
    $('#zoneEditorTitle').textContent = zone ? '구역 편집' : '구역 그리기';
    panel.hidden = false;
    $('#objectInfo').hidden = true;
    $('#layers').open = false;
    view.layers.zones = true;
    $('[data-layer="zones"]').checked = true;
    canvas.classList.add('zone-drawing');
    $('#addZone').classList.add('active');
    sync(); draw(); field('name').focus();
  }
  function rectangle(polygon) {
    // 벤더의 임의 다각형을 사각형으로 바꾸지 않는다.
    const pts = polygon.length === 5 ? polygon.slice(0,4) : polygon;
    if (pts.length !== 4) return null;
    const xs = [...new Set(pts.map(p=>p.x))], ys = [...new Set(pts.map(p=>p.y))];
    if (xs.length !== 2 || ys.length !== 2 || new Set(pts.map(p=>`${p.x},${p.y}`)).size !== 4) return null;
    return {x: Math.min(...xs), y: Math.min(...ys), width: Math.abs(xs[1]-xs[0]), height: Math.abs(ys[1]-ys[0])};
  }
  function pointerDown(e) {
    if (!active) return false;
    if (busy) return true;
    const p = mouse(e), w = bounded(world(p));
    const handle = handles().find(([,x,y])=>Math.abs(x-p.x)<=8 && Math.abs(y-p.y)<=8);
    if (rect && handle) gesture = {mode: handle[0], start: w, rect: {...rect}};
    else if (rect && w.x>=rect.x && w.x<=rect.x+rect.width && w.y>=rect.y && w.y<=rect.y+rect.height)
      gesture = {mode:'move', start:w, rect:{...rect}};
    else if (!rect) {
      const b = bounds(), raw = world(p);
      if (raw.x<b.left || raw.x>b.right || raw.y<b.top || raw.y>b.bottom) return true;
      gesture = {mode:'create', start:w, rect:null};
    } else return true;
    canvas.setPointerCapture(e.pointerId);
    return true;
  }
  function pointerMove(e) {
    if (!active) return false;
    if (busy || !gesture) return true;
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
    if (gesture) {
      pointerMove(e);
      if (rect && (rect.width < minSize() || rect.height < minSize())) rect=gesture.rect;
      gesture=null;
      if(canvas.hasPointerCapture(e.pointerId))canvas.releasePointerCapture(e.pointerId);
      sync(); draw();
    }
    return true;
  }
  function pointerCancel() {
    if (!gesture) return;
    rect=gesture.rect; gesture=null; sync(); draw();
  }
  function drawEditor() {
    if (!active || !rect || !view.image) return;
    const a=point(rect), b=point({x:rect.x+rect.width,y:rect.y+rect.height});
    const color=field('type').value==='elevator'?'#ab9aee':'#f2848c';
    ctx.save(); ctx.fillStyle=color+'45'; ctx.strokeStyle=color; ctx.lineWidth=2;
    ctx.fillRect(a.x,a.y,b.x-a.x,b.y-a.y);
    ctx.setLineDash([6,4]); ctx.strokeRect(a.x,a.y,b.x-a.x,b.y-a.y); ctx.setLineDash([]);
    for (const [,x,y] of handles()) {
      ctx.fillStyle='#f2f5fb'; ctx.fillRect(x-4,y-4,8,8); ctx.strokeRect(x-4,y-4,8,8);
    }
    text(`${rect.width.toFixed(2)} × ${rect.height.toFixed(2)} m`, a.x, a.y-12, color);
    ctx.restore();
  }
  function actions(box, zone) {
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
  field('type').addEventListener('change',()=>draw());
  form.addEventListener('submit',e=>{
    e.preventDefault();
    run(async()=>{
      if (!rect || busy) return;
      const dimensions=Object.fromEntries(dimensionKeys.map(k=>[k,Number(field(k).value)]));
      const data={base_version:baseVersion,name:field('name').value.trim(),type:field('type').value,...dimensions};
      if (!data.name) throw new Error('구역 이름을 입력하세요.');
      const url=`${mapURL()}/zones${zoneId?'/'+enc(zoneId):''}`, method=zoneId?'PUT':'POST';
      const camera={tx:view.tx,ty:view.ty,scale:view.scale};
      busy=true; sync();
      try {
        await request(url,method,data);
        cancel(); await refreshSite(); Object.assign(view,camera); draw();
        notify('구역을 저장하고 새 맵을 적용했습니다.');
      } finally {busy=false; if(active)sync();}
    });
  });
  $('#addZone').onclick=()=>{ $('#addObjectMenu').open=false; start(); };
  $('#cancelZone').onclick=()=>{if(canLeave())run(()=>refreshSite());};
  window.addEventListener('beforeunload',e=>{if(active){e.preventDefault();e.returnValue='';}});
  return {get active(){return active;},get editingId(){return active?zoneId:null;},cancel,canLeave,draw:drawEditor,actions,pointerDown,pointerMove,pointerUp,pointerCancel};
})();
