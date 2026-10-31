'use strict';
// 보관소 배송 1건을 하나의 배송 작업으로 보여준다. 단계 목록은 서버(delivery_progress)가 계산한 값을 그대로 쓴다.
// deliveryView: 관제 화면·보관소 화면 공용 그리기. deliveryUI: 관제 화면의 진행 상세 창 (app.cargo 폴링으로 갱신).
window.deliveryView = (() => {
  const make = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const phases = {queued: '로봇 배정 대기', to_pickup: '픽업 지점 이동', awaiting_bundle: '최종 묶음 확정 대기',
    loading: '트레이 적재', delivering: '세대 배송', returning: '트레이 반납', docking: '충전 스테이션 도킹',
    blocked: '운영자 확인 필요', done: '완료',
    canceled: '배송 취소'};
  const trayStates = {none: '트레이 없음', reserved: '적재 예정', loading: '적재 중', loaded: '적재됨',
    unloading: '하차 중', empty_tray: '빈 트레이', returning: '반납 중', unknown: '확인 필요'};
  const statusMarks = {done: '✓', active: '●', pending: '○', failed: '✕', unknown: '?', canceled: '–'};
  const statusNames = {done: '완료', active: '진행 중', pending: '대기', failed: '확인 필요', unknown: '확인 필요', canceled: '취소'};
  const overall = {active: '진행 중', waiting: '배정 대기', attention: '확인 필요', blocked: '확인 필요', canceled: '취소', done: '완료'};
  const time = at => at ? new Date(at * 1000).toLocaleTimeString('ko-KR') : '';
  function summary(d) {
    const p = d.progress;
    if (!p) return phases[d.phase] || d.phase;
    if (p.status === 'canceled') return '배송 취소';
    return p.status === 'done' ? `완료 · ${p.total}단계` : `${p.done}/${p.total}단계 · ${p.current}`;
  }
  function steps(progress) {
    const list = make('ol', undefined, 'dv-steps');
    for (const step of progress?.steps || []) {
      const item = make('li', undefined, 'dv-step');
      item.dataset.status = step.status;
      const mark = make('span', statusMarks[step.status] || '·', 'dv-mark');
      mark.setAttribute('aria-label', statusNames[step.status] || step.status);
      const body = make('div', undefined, 'dv-body');
      body.append(make('strong', step.label));
      if (step.detail) body.append(make('small', step.detail));
      item.append(mark, body, make('time', time(step.at), 'dv-time'));
      list.append(item);
    }
    return list;
  }
  function trays(d) {
    const box = make('div', undefined, 'dv-trays');
    const slots = d.bundle?.trays || {};
    const items = Object.fromEntries((d.bundle?.items || []).map(i => [i.tray_id, i]));
    for (const tray of ['TRAY_A', 'TRAY_B']) {
      const slot = slots[tray] || {state: 'none'};
      const cell = make('div', undefined, 'rd-tray');
      cell.dataset.state = slot.state;
      cell.append(make('strong', tray), make('span', trayStates[slot.state] || slot.state));
      const item = items[tray];
      if (item) cell.append(make('small', `${item.parcel_id.slice(0, 10)}… · ${item.receipt_no} → ${item.household}`));
      box.append(cell);
    }
    return box;
  }
  function timeline(d) {
    const list = make('ul', undefined, 'dv-timeline');
    for (const entry of d.timeline || []) {
      const stop = entry.phase === 'delivering' && d.stops?.[entry.index] ? ` · ${d.stops[entry.index].household}` : '';
      list.append(make('li', `${time(entry.at)}  ${phases[entry.phase] || entry.phase}${stop}`));
    }
    return list;
  }
  // 하위 작업 이름(locker:J…:phase:index)을 사람이 읽는 단계로 바꾼다.
  function stageName(job, d) {
    const [phase, index] = (job.preset_name || '').split(':').slice(2);
    const stop = phase === 'delivering' ? d?.stops?.[Number(index)]?.household : null;
    return ({to_pickup: '픽업 지점 이동', loading: '트레이 적재', delivering: `세대 하차${stop ? ' ' + stop : ''}`,
      returning: '트레이 반납', docking: '충전 스테이션 도킹'})[phase] || phase || '';
  }
  return {make, phases, trayStates, overall, summary, steps, trays, timeline, time, stageName};
})();

window.deliveryUI = (() => {
  const v = window.deliveryView;
  let dialog = null, openId = null;
  // fms.js 의 전역 const(app, label)는 window 속성이 아니므로 이름으로 참조한다.
  const state = () => typeof app !== 'undefined' ? app : null;
  const find = id => (state()?.cargo?.deliveries || []).find(d => d.job_id === id);
  function forRobot(robotId, job) {
    // 진행 중 배송을 우선한다. 배송이 끝난 직후 하위 작업이 아직 마무리 중이면 그 배송(완료)을 보인다.
    const deliveries = state()?.cargo?.deliveries || [];
    return deliveries.find(d => d.robot_id === robotId && d.phase !== 'done')
      || (job?.external_owner ? deliveries.find(d => d.job_id === job.external_owner) : null) || null;
  }
  function children(id) {
    return (state()?.jobs || []).filter(j => j.external_owner === id);
  }
  function render() {
    if (!dialog || !openId) return;
    const d = find(openId), body = dialog.querySelector('.dv-content');
    body.replaceChildren();
    if (!d) { body.append(v.make('p', '배송 정보를 찾을 수 없습니다. 목록에서 사라졌을 수 있습니다.', 'muted')); return; }
    const p = d.progress || {};
    const head = v.make('header', undefined, 'dv-head');
    const badge = v.make('span', v.overall[p.status] || d.phase, 'dv-badge');
    badge.dataset.status = p.status || d.phase;
    head.append(v.make('h2', `배송 ${d.job_id}`), badge);
    body.append(head);
    body.append(v.make('p', [d.robot_id ? `로봇 ${d.robot_id}` : '로봇 미배정', d.bundle_id ? `묶음 ${d.bundle_id.slice(0, 12)}…` : null,
      p.started_at ? `시작 ${v.time(p.started_at)}` : null, p.finished_at ? `종료 ${v.time(p.finished_at)}` : null]
      .filter(Boolean).join(' · '), 'muted'));
    const meter = v.make('progress');
    meter.max = p.total || 1; meter.value = p.done || 0;
    meter.setAttribute('aria-label', '배송 단계 진행률');
    body.append(meter);
    if (d.error) {
      const warn = v.make('div', undefined, 'dv-error');
      const title = d.phase === 'blocked' ? '운영자 확인 필요' : d.phase === 'canceled' ? '취소됨'
        : p.status === 'attention' ? '확인 필요 — 로봇 배정 전' : '대기 사유';
      warn.append(v.make('strong', title), v.make('p', d.error));
      if (p.status === 'attention') {
        warn.append(v.make('p', '보관소 배송 화면에서 세대 목적지를 연결하면 이어서 진행합니다. '
          + '연결할 수 없으면 작업 게시판에서 [배송 취소]를 누르세요. 갈 수 없는 세대 물품은 직접 수령으로 바뀝니다.'));
        const link = v.make('a', '보관소 배송 화면에서 세대 연결 →');
        link.href = '/static/locker.html';
        warn.append(link);
      }
      if (d.phase === 'blocked') {
        const link = v.make('a', '보관소 배송 화면에서 적재물 확인·복귀 →');
        link.href = '/static/locker.html';
        warn.append(link);
      }
      body.append(warn);
    }
    const grid = v.make('div', undefined, 'dv-grid');
    const left = v.make('section'), right = v.make('section');
    left.append(v.make('h3', '진행 단계'), v.steps(p));
    right.append(v.make('h3', '트레이'), v.trays(d), v.make('h3', '단계 전환 기록'), v.timeline(d));
    const jobs = children(d.job_id);
    if (jobs.length) {
      right.append(v.make('h3', '하위 작업'));
      const list = v.make('ul', undefined, 'dv-timeline');
      for (const j of jobs) list.append(v.make('li', `${j.job_id} · ${statusLabel(j.status)} · ${v.stageName(j, d)}`));
      right.append(list);
    }
    grid.append(left, right);
    body.append(grid);
  }
  function statusLabel(status) {
    // fms.js 의 전역 label(작업 상태 한글명). 보관소 화면에는 없다.
    return typeof label === 'function' ? label(status) : status;
  }
  function open(id) {
    openId = id;
    if (!dialog) {
      dialog = document.createElement('dialog');
      dialog.className = 'panel delivery-dialog';
      const close = v.make('button', '닫기');
      close.type = 'button';
      close.onclick = () => dialog.close();
      dialog.append(close, v.make('div', undefined, 'dv-content'));
      dialog.addEventListener('close', () => { openId = null; });
      document.body.append(dialog);
    }
    render();
    if (!dialog.open) dialog.showModal();
  }
  return {open, refresh: render, forRobot, children, get openId() { return openId; }};
})();
