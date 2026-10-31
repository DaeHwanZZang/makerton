const el = id => document.getElementById(id);
const state = { key: '', parcels: [], stats: null, robots: [] };
const safe = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
const STATUS = { STORED: '보관 완료', ROBOT_PREPARING: '로봇 운송 준비 중', IN_TRANSIT: '로봇 배송 중', DELIVERED: '배송 완료', EXCEPTION: '확인 필요' };
const NEXT = { STORED: 'DELIVERED', IN_TRANSIT: 'DELIVERED', DELIVERED: null };
function displayStatus(parcel) {
  return parcel.status === 'STORED' && parcel.deliveryMethod === 'ROBOT' ? 'ROBOT_PREPARING' : parcel.status;
}
function error(message = '') { el('admin-error').textContent = message; el('admin-error').hidden = !message; }
async function api(url, opts = {}) {
  const resp = await fetch(url, { ...opts, headers: { 'x-admin-key': state.key, ...(opts.headers || {}) } });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data?.error?.message || '요청에 실패했습니다.');
  return data;
}
function badge(parcel) { const status = displayStatus(parcel); return `<span class="status-pill status-${safe(status)}">${safe(STATUS[status] || status)}</span>`; }
function renderRobots() {
  el('admin-robots').innerHTML = state.robots.length ? state.robots.map(robot => `<article class="admin-robot-item ${robot.connected ? '' : 'robot-offline'}"><div><span class="robot-connection-dot"></span><strong>${safe(robot.id)}</strong><small>${safe(robot.connectionMode)} · ${safe(robot.location)} · 배터리 ${safe(robot.battery)}%</small><code>${safe(robot.mqttTopic)}</code></div><div class="row-actions"><button class="tiny-btn" data-robot-online="${safe(robot.id)}" type="button">정상 통신</button><button class="tiny-btn tiny-danger" data-robot-offline="${safe(robot.id)}" type="button">고장·통신 끊김</button></div></article>`).join('') : '<div class="empty-table">등록된 로봇이 없습니다.</div>';
}
function render() {
  const counts = state.stats?.byDisplayStatus || {};
  const tiles = [
    ['전체 택배', state.stats?.total || 0, 'all'],
    ['보관 완료', counts.STORED || 0, 'stored'],
    ['로봇 운송 준비 중', counts.ROBOT_PREPARING || 0, 'pending'],
    ['배송 완료', counts.DELIVERED || 0, 'delivered']
  ];
  el('stats-grid').innerHTML = tiles.map(t => `<div class="stat-card"><span class="stat-label">${t[0]}</span><strong>${t[1].toLocaleString('ko-KR')}</strong><span class="stat-symbol stat-${t[2]}">${{ all: '◈', pending: '↗', stored: '▣', delivered: '✓' }[t[2]]}</span></div>`).join('');
  const filtered = state.parcels.filter(p => !el('status-filter').value || displayStatus(p) === el('status-filter').value);
  el('parcels-body').innerHTML = filtered.length ? filtered.map(p => `<tr><td><strong class="mono">${safe(p.id)}</strong><small class="table-secondary">${safe(p.trackingNumber || '송장번호 없음')}</small></td><td><strong>${safe(p.building)}동 ${safe(p.unit)}호</strong></td><td>${safe(p.size)}</td><td>${safe(p.sortZone)}</td><td>${badge(p)}</td><td>${new Date(p.createdAt).toLocaleString('ko-KR')}</td><td><div class="row-actions">${NEXT[p.status] ? `<button class="tiny-btn" data-next="${safe(p.id)}" data-status="${NEXT[p.status]}">${safe(STATUS[NEXT[p.status]])} →</button>` : ''}<button class="tiny-btn tiny-outline" data-events="${safe(p.id)}">이력</button></div></td></tr>`).join('') : '<tr><td colspan="7" class="empty-table">조회된 택배가 없습니다. 키오스크에서 먼저 택배를 등록해 보세요.</td></tr>';
  renderRobots();
}
async function refresh() {
  error();
  try {
    const [list, stats, robots] = await Promise.all([api('/api/parcels'), api('/api/stats'), api('/api/admin/robots')]);
    state.parcels = list.parcels;
    state.stats = stats;
    state.robots = robots.robots;
    render();
  } catch (err) { error(err.message); }
}
el('admin-login-form').addEventListener('submit', async event => {
  event.preventDefault();
  state.key = el('admin-key').value.trim();
  try {
    const [list, stats, robots] = await Promise.all([api('/api/parcels'), api('/api/stats'), api('/api/admin/robots')]);
    state.parcels = list.parcels;
    state.stats = stats;
    state.robots = robots.robots;
    el('admin-login').hidden = true;
    el('admin-dashboard').hidden = false;
    el('refresh-btn').hidden = false;
    error();
    render();
  } catch (err) { error(err.message); }
});
el('refresh-btn').addEventListener('click', refresh);
el('status-filter').addEventListener('change', render);
document.addEventListener('click', async event => {
  const online = event.target.closest('[data-robot-online]');
  const offline = event.target.closest('[data-robot-offline]');
  if (online || offline) {
    const button = online || offline;
    button.disabled = true;
    const isOnline = Boolean(online);
    try {
      await api(`/api/admin/robots/${encodeURIComponent(isOnline ? online.dataset.robotOnline : offline.dataset.robotOffline)}`, {
        method: 'PATCH', headers: { 'content-type': 'application/json' },
        body: JSON.stringify(isOnline
          ? { connectionMode: 'SIMULATED', available: true, status: '순찰 중', location: '공동 보관소', battery: 92 }
          : { connectionMode: 'OFFLINE', available: false, status: '점검 중', location: '정비소', battery: 30 })
      });
      await refresh();
    } catch (err) { error(err.message); button.disabled = false; }
    return;
  }
  const next = event.target.closest('[data-next]');
  if (next) {
    let lockerId = null;
    if (next.dataset.status === 'STORED') {
      const entered = window.prompt('택배를 넣은 실제 보관 위치를 입력해 주세요. (예: A-01)');
      if (entered === null) return;
      lockerId = entered.trim().toUpperCase();
      if (!/^[A-Z0-9-]{1,24}$/.test(lockerId)) {
        error('실제 보관 위치를 영문·숫자·하이픈 1~24자로 입력해 주세요.');
        return;
      }
    }
    next.disabled = true;
    try {
      await api(`/api/parcels/${encodeURIComponent(next.dataset.next)}/status`, {
        method: 'PATCH', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ status: next.dataset.status, actor: 'admin-demo', lockerId })
      });
      await refresh();
    } catch (err) { error(err.message); next.disabled = false; }
  }
  const events = event.target.closest('[data-events]');
  if (events) {
    try {
      const result = await api(`/api/parcels/${encodeURIComponent(events.dataset.events)}/events`);
      window.alert(result.events.map(e => `${new Date(e.createdAt).toLocaleString('ko-KR')} | ${STATUS[e.toStatus]} (${e.actor})`).join('\n'));
    } catch (err) { error(err.message); }
  }
});
