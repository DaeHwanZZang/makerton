'use strict';
// 주기 갱신은 입력 중인 제한값을 덮어쓰지 않는다.
window.trafficView = (() => {
  let current = '', dirty = false, busy = false, snapshot = null, generation = 0;
  const input = $('#trafficLimit');
  input.addEventListener('input', () => { dirty = true; });
  async function refresh() {
    const token = ++generation;
    const key = app.site && app.map ? mapURL() : '';
    if (key !== current) { current = key; dirty = false; snapshot = null; input.value = ''; }
    $('#trafficForm').querySelector('button').disabled = !key || busy;
    input.disabled = !key || busy;
    $('#trafficMapName').textContent = key ? `${app.site} / ${app.map}` : '상단에서 현장과 맵을 선택하세요.';
    if (!key) { $('#trafficStatus').replaceChildren(); return; }
    const result = await request(`${key}/traffic`);
    if (token !== generation || key !== current || key !== mapURL()) return;
    snapshot = result;
    if (!dirty) input.value = result.max_moving_robots ?? '';
    const box = $('#trafficStatus'); box.replaceChildren();
    const names = {free:'비어 있음',reserved:'진입 예약',occupied:'점유 중',blocked:'확인 필요 · 신규 진입 차단'};
    for (const zone of result.zones) box.append(el('p',`${zone.name || zone.zone_id} · ${names[zone.status]}${zone.owner ? ' · '+zone.owner : ''}`));
    for (const lease of result.leases) {
      const label = lease.kind === 'floor' ? '이동 허가' : lease.kind === 'wait' ? '대기 node 예약' : '점유 존 예약';
      box.append(el('p', `${lease.robot} · ${label}${lease.future ? ' (도착 층)' : ''} · ${lease.resource.split(':').at(-1)}`));
    }
    for (const waiting of result.waiting) box.append(el('p', `${waiting.robot_id} · ${waiting.reason}`));
    if (!result.leases.length && !result.waiting.length) box.append(el('p','예약·대기 중인 로봇이 없습니다.','muted'));
    draw();
  }
  $('#trafficForm').addEventListener('submit', e => {
    e.preventDefault();
    run(async () => {
      if (!current || busy) return;
      const key = current, value = input.value === '' ? null : Number(input.value);
      if (!app.site || !app.map || key !== mapURL()) {
        await refresh();
        throw new Error('선택 맵이 바뀌었습니다. 설정값을 다시 확인하세요.');
      }
      if (value !== null && (!Number.isInteger(value) || value < 1)) throw new Error('1 이상의 정수 또는 빈칸을 입력하세요.');
      busy = true; ++generation;
      try {
        await request(`${key}/traffic`, 'PUT', {max_moving_robots:value});
        if (current === key) dirty = false;
        notify('층별 동시 이동 대수 제한을 저장했습니다.');
      } finally { busy = false; }
      await refresh();
    });
  });
  setInterval(() => { if (!busy) run(refresh); }, 1500);
  return {refresh, get snapshot(){return app.site && app.map && current===mapURL() ? snapshot : null;}};
})();
