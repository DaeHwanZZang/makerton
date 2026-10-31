'use strict';
// 엘리베이터 연결 화면: 층(맵)마다 그린 엘리베이터 구역 중 "같은 승강기" 를 직접 골라 짝짓는다.
// fms.js 의 전역($, el, request, run, notify, app, siteURL, titles, showPage)을 그대로 쓴다.
(() => {
  const S = {key:'', floors:[], elevators:[], pick:new Map(), editing:null, linked:new Map()};
  const COLORS = {free:'#6aabff', picked:'#64c8a5', other:'#6b7a8f', hover:'#e3ba70'};

  const baseOf = id => `${siteURL()}/maps/${enc(id)}`;
  const slug = n => (n || '').trim().replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60);

  async function load() {
    if (!app.site) { S.floors = []; S.elevators = []; render(); return; }
    const site = app.site;
    const [{elevators}, maps] = await Promise.all([request(`${siteURL()}/elevators`), Promise.resolve(app.detail?.maps || [])]);
    const floors = [];
    for (const m of maps) {
      if (!m.active_version) continue;
      const file = n => `${baseOf(m.map_id)}/files/${enc(n)}?version=${enc(m.active_version)}`;
      const [meta, ann] = await Promise.all([request(file('fms_map.json')), request(file('annotations.json'))]);
      const zones = ann.zones.filter(z => z.type === 'elevator').map(z => {
        const xs = z.polygon.map(p => p.x), ys = z.polygon.map(p => p.y);
        return {id:z.zone_id, name:z.name, x0:Math.min(...xs), x1:Math.max(...xs), y0:Math.min(...ys), y1:Math.max(...ys)};
      });
      const image = new Image();
      await new Promise((res, rej) => { image.onload = res; image.onerror = () => rej(new Error(`${m.map_id} 맵 이미지를 불러오지 못했습니다.`)); image.src = file(meta.image); });
      floors.push({map_id:m.map_id, meta, image, zones, canvas:null, hover:null});
    }
    if (site !== app.site) return;
    S.floors = floors;
    S.elevators = elevators;
    S.linked = new Map();  // `${map}|${zone}` -> 승강기 이름
    for (const e of elevators) for (const s of e.stops) S.linked.set(`${s.map_id}|${s.zone_id}`, e.name || e.elevator_id);
    render();
  }

  function usable(floor, zone) {  // 다른 승강기에 묶여 있지 않거나, 지금 편집 중인 승강기 것
    const owner = S.linked.get(`${floor.map_id}|${zone.id}`);
    return !owner || (S.editing && owner === (S.elevators.find(e => e.elevator_id === S.editing)?.name || S.editing));
  }

  function draw(f) {
    const c = f.canvas, ctx = c.getContext('2d'), m = f.meta;
    const zs = f.zones;
    if (!zs.length) { ctx.clearRect(0, 0, c.width, c.height); return; }
    const pad = 3;  // m
    const x0 = Math.min(...zs.map(z => z.x0)) - pad, x1 = Math.max(...zs.map(z => z.x1)) + pad;
    const y0 = Math.min(...zs.map(z => z.y0)) - pad, y1 = Math.max(...zs.map(z => z.y1)) + pad;
    const sc = c.width / (x1 - x0);
    c.height = Math.ceil((y1 - y0) * sc);  // 폭에 맞추고 높이는 구역이 퍼진 범위에 맞춘다
    f.view = {x0, y0, sc};
    ctx.fillStyle = '#0d131c'; ctx.fillRect(0, 0, c.width, c.height);
    ctx.imageSmoothingEnabled = false;
    const res = m.resolution;
    ctx.drawImage(f.image, (x0 - m.origin_x) / res, (y0 - m.origin_y) / res, (x1 - x0) / res, (y1 - y0) / res,
      0, 0, (x1 - x0) * sc, (y1 - y0) * sc);
    ctx.font = '12px sans-serif'; ctx.textBaseline = 'top';
    for (const z of zs) {
      const free = usable(f, z), picked = S.pick.get(f.map_id)?.zone_id === z.id, hover = f.hover === z.id;
      const col = picked ? COLORS.picked : hover && free ? COLORS.hover : free ? COLORS.free : COLORS.other;
      const px = (z.x0 - x0) * sc, py = (z.y0 - y0) * sc, w = (z.x1 - z.x0) * sc, h = (z.y1 - z.y0) * sc;
      ctx.globalAlpha = picked ? .45 : .25; ctx.fillStyle = col; ctx.fillRect(px, py, w, h);
      ctx.globalAlpha = 1; ctx.strokeStyle = col; ctx.lineWidth = picked ? 3 : 2; ctx.strokeRect(px, py, w, h);
      const owner = S.linked.get(`${f.map_id}|${z.id}`);
      const label = free || picked ? z.name : `${z.name} → ${owner}`;
      ctx.fillStyle = '#000'; ctx.fillText(label, px + 3, py + 3 + 1); ctx.fillStyle = '#fff'; ctx.fillText(label, px + 2, py + 2);
    }
  }

  function hit(f, ev) {
    const r = f.canvas.getBoundingClientRect(), v = f.view;
    if (!v) return null;
    const x = v.x0 + (ev.clientX - r.left) * (f.canvas.width / r.width) / v.sc;
    const y = v.y0 + (ev.clientY - r.top) * (f.canvas.height / r.height) / v.sc;
    return f.zones.find(z => x >= z.x0 && x <= z.x1 && y >= z.y0 && y <= z.y1) || null;
  }

  function floorCard(f) {
    const card = el('div', undefined, 'elev-floor');
    const head = el('div', undefined, 'elev-floor-head');
    head.append(el('b', f.map_id));
    const floor = el('input'); floor.placeholder = '층 이름 (예: 1F)'; floor.maxLength = 32;
    floor.value = S.pick.get(f.map_id)?.floor || '';
    floor.oninput = () => { const p = S.pick.get(f.map_id); if (p) p.floor = floor.value.trim(); };
    floor.disabled = !S.pick.has(f.map_id);
    const status = el('span', undefined, 'muted');
    const upd = () => {
      const p = S.pick.get(f.map_id), z = p && f.zones.find(q => q.id === p.zone_id);
      status.textContent = z ? `선택: ${z.name}` : '이 층에 서지 않음 (구역을 클릭해 선택)';
      floor.disabled = !p;
    };
    head.append(floor, status);
    const c = f.canvas = el('canvas'); c.width = 1000; c.height = 400;
    c.className = 'elev-canvas';
    c.onmousemove = ev => { const z = hit(f, ev); const id = z && usable(f, z) ? z.id : null; if (id !== f.hover) { f.hover = id; c.style.cursor = id ? 'pointer' : 'default'; draw(f); } };
    c.onmouseleave = () => { f.hover = null; draw(f); };
    c.onclick = ev => {
      const z = hit(f, ev);
      if (!z) return;
      if (!usable(f, z)) { notify(`${z.name} 는 이미 승강기 '${S.linked.get(`${f.map_id}|${z.id}`)}' 에 묶여 있습니다.`, true); return; }
      const cur = S.pick.get(f.map_id);
      if (cur?.zone_id === z.id) S.pick.delete(f.map_id);   // 다시 누르면 해제
      else S.pick.set(f.map_id, {zone_id:z.id, floor:floor.value.trim()});
      upd(); draw(f); refreshSave();
    };
    card.append(head, f.zones.length ? c : el('p', '이 맵에는 엘리베이터 구역이 없습니다. 실시간 맵에서 구역을 추가하세요.', 'muted'));
    upd();
    return card;
  }

  let saveBtn, nameIn, idIn, hint;
  function autoId() {  // 한글 이름처럼 ID 로 못 쓰는 이름이면 elev-1, elev-2 ... 중 빈 번호
    const s = slug(nameIn.value);
    if (s) return s;
    let n = 1;
    while (S.elevators.some(e => e.elevator_id === `elev-${n}`)) n++;
    return `elev-${n}`;
  }

  function refreshSave() {
    const n = S.pick.size;
    saveBtn.disabled = n < 2 || !nameIn.value.trim();
    hint.textContent = n < 2 ? `층을 2개 이상 선택해야 합니다. (현재 ${n}개)` : `${n}개 층을 하나의 승강기로 묶습니다.`;
  }

  function reset() {
    S.editing = null; S.pick = new Map();
    render();
  }

  function render() {
    const root = $('#elevatorBody');
    if (!root) return;
    root.replaceChildren();
    if (!app.site) { root.append(el('p', '현장을 먼저 선택하세요.', 'muted')); return; }

    const panel = el('div', undefined, 'panel');
    panel.append(el('h3', S.editing ? `승강기 수정: ${S.editing}` : '승강기 만들기'));
    panel.append(el('p', '층(맵)마다 같은 승강기에 해당하는 구역을 한 개씩 클릭해 짝지으세요. 파란 구역은 선택 가능, 회색은 이미 다른 승강기에 묶인 구역입니다.', 'muted'));
    const bar = el('div', undefined, 'elev-form');
    nameIn = el('input'); nameIn.placeholder = '승강기 이름 (예: 1호기)'; nameIn.maxLength = 128;
    idIn = el('input'); idIn.placeholder = 'ID (영문/숫자, 비우면 자동: elev-1…)'; idIn.disabled = !!S.editing;
    saveBtn = el('button', S.editing ? '수정 저장' : '승강기 저장', 'primary'); saveBtn.type = 'button';
    const cancel = el('button', '취소'); cancel.type = 'button'; cancel.hidden = !S.editing; cancel.onclick = reset;
    hint = el('span', undefined, 'muted');
    if (S.editing) { const e = S.elevators.find(x => x.elevator_id === S.editing); nameIn.value = e?.name || ''; idIn.value = S.editing; }
    nameIn.oninput = idIn.oninput = refreshSave;
    saveBtn.onclick = () => run(async () => {
      const stops = [...S.pick].map(([map_id, p]) => ({map_id, zone_id:p.zone_id, floor:p.floor || ''}));
      const name = nameIn.value.trim();
      if (S.editing) await request(`${siteURL()}/elevators/${enc(S.editing)}`, 'PUT', {name, stops});
      else await request(`${siteURL()}/elevators`, 'POST', {elevator_id:idIn.value.trim() || autoId(), name, stops});
      notify(S.editing ? '승강기를 수정했습니다.' : '승강기를 저장했습니다.');
      S.editing = null; S.pick = new Map(); await load();
    });
    bar.append(nameIn, idIn, saveBtn, cancel, hint);
    panel.append(bar);
    const grid = el('div', undefined, 'elev-floors');
    for (const f of S.floors) grid.append(floorCard(f));
    if (!S.floors.length) grid.append(el('p', '활성 버전이 있는 맵이 없습니다.', 'muted'));
    panel.append(grid);
    root.append(panel);

    root.append(el('h3', '연결된 승강기'));
    const wrap = el('div', undefined, 'table-wrap'), table = el('table');
    table.innerHTML = '<thead><tr><th>이름</th><th>ID</th><th>서는 층 (맵 · 구역)</th><th></th></tr></thead>';
    const body = el('tbody');
    for (const e of S.elevators) {
      const tr = el('tr');
      tr.append(el('td', e.name), el('td', e.elevator_id));
      const td = el('td');
      for (const s of e.stops) {
        const chip = el('span', `${s.floor ? s.floor + ' · ' : ''}${s.map_id} · ${s.zone_name ?? s.zone_id}${s.missing ? ' (구역 없음!)' : ''}`, s.missing ? 'chip bad' : 'chip');
        td.append(chip);
      }
      const act = el('td');
      const edit = el('button', '수정'); edit.type = 'button';
      edit.onclick = () => { S.editing = e.elevator_id; S.pick = new Map(e.stops.filter(s => !s.missing).map(s => [s.map_id, {zone_id:s.zone_id, floor:s.floor}])); render(); scrollTo({top:0}); };
      const del = el('button', '삭제'); del.type = 'button';
      del.onclick = () => run(async () => {
        if (!confirm(`승강기 '${e.name}' 의 연결을 삭제할까요? (구역은 그대로 남습니다)`)) return;
        await request(`${siteURL()}/elevators/${enc(e.elevator_id)}`, 'DELETE'); notify('연결을 삭제했습니다.');
        if (S.editing === e.elevator_id) { S.editing = null; S.pick = new Map(); }
        await load();
      });
      act.append(edit, del);
      tr.append(td, act);
      body.append(tr);
    }
    if (!S.elevators.length) { const tr = el('tr'), td = el('td', '아직 연결한 승강기가 없습니다.', 'empty-row'); td.colSpan = 4; tr.append(td); body.append(tr); }
    table.append(body); wrap.append(table); root.append(wrap);
    for (const f of S.floors) if (f.canvas) draw(f);
    refreshSave();
  }

  // 현장/맵 버전이 바뀌거나 이 화면을 처음 열 때만 다시 읽는다 (선택 중인 내용을 주기 갱신이 지우지 않게)
  setInterval(() => {
    if (app.page !== 'elevators') { S.key = ''; return; }
    const key = app.site + '|' + (app.detail?.maps || []).map(m => `${m.map_id}:${m.active_version}`).join(',');
    if (key !== S.key) { S.key = key; S.pick = new Map(); S.editing = null; run(load); }
  }, 700);
  $('#refresh')?.addEventListener('click', () => { if (app.page === 'elevators') run(load); });
})();
