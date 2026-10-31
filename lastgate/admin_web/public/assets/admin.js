// LastGate 관리자 웹 화면. 모든 요청은 관리자 웹 서버(/api/admin)를 거쳐 보관소 서버로 간다.
const IDLE_LIMIT_MS = 30 * 60 * 1000;   // 30분 동안 조작이 없으면 로그아웃 (서버도 같은 기준으로 만료)
const state = {
  me: null, site: null, publicSite: null, defaults: null, accountStats: null, activeItems: 0,
  page: null, previewTimer: null, previewSeq: 0, storage: null, storageTimer: null, lastActivity: Date.now()
};
const el = id => document.getElementById(id);
const safe = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
const fmt = iso => (iso ? new Date(iso).toLocaleString('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '-');

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers }
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body?.error?.message || '요청을 처리하지 못했습니다.');
    Object.assign(error, { status: response.status, code: body?.error?.code, detail: body?.error || {} });
    if (response.status === 401 && path !== '/api/admin/login' && error.code !== 'DENIED') showLogin('로그인이 만료되었습니다. 다시 로그인하세요.');
    throw error;
  }
  return body;
}
const post = (path, body = {}, method = 'POST') => api(path, { method, body: JSON.stringify(body) });

function showAlert(id, message = '', kind = 'error') {
  const node = el(id);
  node.textContent = message;
  node.hidden = !message;
  node.className = `alert alert-${kind}`;
}

let toastTimer;
function toast(message) {
  el('toast').textContent = message;
  el('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el('toast').hidden = true; }, 3500);
}

// --- 로그인·세션 -----------------------------------------------------------
function showLogin(message = '') {
  state.me = null;
  stopStorageTimer();
  el('app').hidden = true;
  el('login-view').hidden = false;
  showAlert('login-alert', message, 'warn');
  el('login-form').password.value = '';
  el('login-form').password.focus();
}

async function handleLogin(form) {
  showAlert('login-alert');
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    state.me = await post('/api/admin/login', { adminId: form.adminId.value.trim(), password: form.password.value });
    form.password.value = '';
    await enterApp();
  } catch (error) {
    const left = error.detail?.remaining;
    showAlert('login-alert', error.message + (left ? ` (남은 시도 ${left}번)` : ''));
  } finally { button.disabled = false; }
}

async function enterApp() {
  el('login-view').hidden = true;
  el('app').hidden = false;
  state.lastActivity = Date.now();
  await loadSite();
  if (state.me?.default_password) return openPage('admin-password');
  openPage(state.site ? 'storage' : 'init');
}

async function loadSite() {
  const data = await api('/api/admin/site');
  state.me = { ...(state.me || {}), default_password: data.default_password, initialized: data.initialized, site_name: data.site_name };
  state.site = data.site;
  state.publicSite = data.public;
  state.defaults = data.defaults;
  state.accountStats = data.accounts;
  state.activeItems = data.active_items;
  renderChrome();
}

function renderChrome() {
  const initialized = Boolean(state.site);
  el('site-label').textContent = initialized ? `${state.site.name} · v${state.site.version}` : '초기 설정 전';
  el('default-password-banner').hidden = !state.me?.default_password;
  document.querySelectorAll('#menu [data-need]').forEach(button => {
    button.hidden = button.dataset.need === 'init' ? !initialized : initialized;
  });
  const terms = state.site || { building_term: '건물', unit_term: '호실' };
  document.querySelectorAll('.building-term').forEach(node => { node.textContent = terms.building_term; });
  document.querySelectorAll('.unit-term').forEach(node => { node.textContent = terms.unit_term; });
}

// --- 화면 전환 -----------------------------------------------------------
const PAGE_SECTIONS = {
  init: 'page-site', settings: 'page-site', accounts: 'page-accounts', 'reset-password': 'page-account-action',
  occupant: 'page-account-action', storage: 'page-storage', 'code-pickup': 'page-code-pickup', events: 'page-events',
  'factory-reset': 'page-factory-reset', 'admin-password': 'page-admin-password'
};

function openPage(page) {
  if (!state.site && !['init', 'events', 'admin-password'].includes(page)) page = 'init';
  if (state.site && page === 'init') page = 'settings';
  state.page = page;
  stopStorageTimer();
  document.querySelectorAll('.page').forEach(section => { section.hidden = section.id !== PAGE_SECTIONS[page]; });
  document.querySelectorAll('[data-page]').forEach(button => button.classList.toggle('active', button.dataset.page === page));
  window.scrollTo({ top: 0 });
  ({
    init: () => renderSiteForm(), settings: () => renderSiteForm(), accounts: () => loadAccounts(),
    'reset-password': () => renderAccountAction('reset'), occupant: () => renderAccountAction('occupant'),
    storage: () => { loadStorage(); state.storageTimer = setInterval(loadStorage, 10000); },
    'code-pickup': () => { el('code-result').hidden = true; el('code-form').reset(); el('code-form').code.focus(); },
    events: () => loadEvents(),
    'factory-reset': () => { el('reset-name').textContent = state.site?.name || ''; el('reset-form').reset(); showAlert('reset-alert'); },
    'admin-password': () => { el('admin-password-form').reset(); showAlert('admin-password-alert'); }
  })[page]?.();
}

function stopStorageTimer() {
  clearInterval(state.storageTimer);
  state.storageTimer = null;
}

// --- 현장 설정 (초기 설정·설정 변경) -------------------------------------
function blankBuilding(code) {
  return { code, name: '', unit_from: '', unit_to: '', missing: '', extra: '' };
}

function renderSiteForm() {
  const form = el('site-form');
  const initialized = Boolean(state.site);
  const source = state.site || {
    name: '', building_term: '건물', unit_term: '호실', buildings: [blankBuilding(1)], ...state.defaults
  };
  el('site-title').textContent = initialized ? '설정 변경' : '초기 설정';
  el('site-desc').textContent = initialized
    ? `현재 v${state.site.version}. 저장하면 키오스크·세대 웹이 한가한 때에 새 설정을 불러옵니다. 뺀 호실의 계정은 비활성화되고, 다시 넣으면 새 입주자로 초기화됩니다.`
    : '건물과 호실을 정하면 호실마다 세대 계정(초기 비밀번호 1234)이 만들어집니다. 저장 전까지 키오스크와 세대 웹은 쓸 수 없습니다.';
  el('site-submit').textContent = initialized ? '설정 변경 저장' : '초기 설정 저장';
  el('site-revert').hidden = !initialized;
  for (const key of ['name', 'building_term', 'unit_term', 'grid_bins', 'oversize_doors', 'robots', 'service_start', 'service_end']) {
    form[key].value = source[key] ?? '';
  }
  form.second_seat_same_building.checked = Boolean(source.second_seat_same_building);
  el('buildings').innerHTML = '';
  source.buildings.forEach(b => addBuildingRow(b, initialized && state.site.buildings.some(x => x.code === b.code)));
  schedulePreview(0);
}

function addBuildingRow(b, locked = false) {
  const row = document.createElement('div');
  row.className = 'building-row';
  row.innerHTML = `
    <label class="code">번호<input data-field="code" type="number" min="1" max="999" value="${safe(b.code)}" ${locked ? 'readonly title="만든 건물의 번호는 바꿀 수 없습니다"' : ''} required></label>
    <label class="name">이름<input data-field="name" maxlength="20" value="${safe(b.name)}" placeholder="예: 101동, 공학관" required></label>
    <label class="unit">호실 시작<input data-field="unit_from" type="number" min="101" max="9999" value="${safe(b.unit_from)}" placeholder="101" required></label>
    <label class="unit">호실 끝<input data-field="unit_to" type="number" min="101" max="9999" value="${safe(b.unit_to)}" placeholder="614" required></label>
    <label class="wide">없는 호실<input data-field="missing" value="${safe(b.missing)}" placeholder="예: 404, 501~514"></label>
    <label class="wide">추가 호실<input data-field="extra" value="${safe(b.extra)}" placeholder="예: 1501"></label>
    <button type="button" class="btn btn-icon" data-remove-building title="건물 빼기" aria-label="건물 빼기">✕</button>
    <p class="building-guide hint"></p>`;
  el('buildings').append(row);
  updateRemoveButtons();
}

function updateRemoveButtons() {
  const rows = el('buildings').querySelectorAll('.building-row');
  rows.forEach(row => { row.querySelector('[data-remove-building]').disabled = rows.length <= 1; });
}

function readSite() {
  const form = el('site-form');
  const value = key => form[key].value.trim();
  return {
    name: value('name'), building_term: value('building_term'), unit_term: value('unit_term'),
    buildings: [...el('buildings').querySelectorAll('.building-row')].map(row => Object.fromEntries(
      [...row.querySelectorAll('[data-field]')].map(input => [input.dataset.field, input.value.trim()]))),
    grid_bins: value('grid_bins'), oversize_doors: value('oversize_doors'), robots: value('robots'),
    service_start: value('service_start'), service_end: value('service_end'),
    second_seat_same_building: form.second_seat_same_building.checked
  };
}

function schedulePreview(delay = 400) {
  clearTimeout(state.previewTimer);
  state.previewTimer = setTimeout(runPreview, delay);
}

async function runPreview() {
  const seq = ++state.previewSeq;
  const body = el('preview-body');
  const site = readSite();
  if (!site.name || site.buildings.some(b => !b.name || !b.unit_from || !b.unit_to)) {
    body.innerHTML = '<p class="hint">사이트 이름과 건물마다 이름·호실 범위를 채우면 미리보기가 나옵니다.</p>';
    el('site-submit').disabled = true;
    return;
  }
  try {
    const result = await post('/api/admin/site/preview', { site });
    if (seq !== state.previewSeq) return;
    state.preview = result;
    const rows = result.buildings.map(b => `<tr><td>${safe(b.code)}</td><td>${safe(b.name)}</td><td class="num">${b.count.toLocaleString()}</td><td>${safe(b.guide)}</td></tr>`).join('');
    const diff = result.diff;
    body.innerHTML = `<p class="big">세대 계정 <b>${result.account_count.toLocaleString()}</b>개${diff ? ` · 추가 <b class="plus">+${diff.added}</b> · 제외 <b class="minus">−${diff.removed}</b>` : ''}</p>
      ${diff?.blocked?.length ? `<div class="alert alert-error">${diff.blocked.map(safe).join('<br>')}</div>` : ''}
      <table class="table compact"><thead><tr><th>번호</th><th>이름</th><th class="num">호실 수</th><th>키오스크 안내 문구</th></tr></thead><tbody>${rows}</tbody></table>`;
    el('site-submit').disabled = Boolean(diff?.blocked?.length);
    // 건물별 안내 문구를 행 아래에도 보여 준다
    el('buildings').querySelectorAll('.building-row').forEach((row, i) => {
      const info = result.buildings[i];
      row.querySelector('.building-guide').textContent = info ? `${info.count}개 · ${info.guide}` : '';
    });
  } catch (error) {
    if (seq !== state.previewSeq) return;
    body.innerHTML = `<div class="alert alert-error">${safe(error.message)}</div>`;
    el('site-submit').disabled = true;
  }
}

async function submitSite() {
  const site = readSite();
  const initialized = Boolean(state.site);
  const preview = state.preview;
  const message = initialized
    ? `설정을 저장할까요?\n호실 추가 ${preview?.diff?.added ?? 0}개 · 제외 ${preview?.diff?.removed ?? 0}개\n제외한 호실의 계정은 비활성화됩니다.`
    : `초기 설정을 저장할까요?\n세대 계정 ${preview?.account_count?.toLocaleString() ?? '?'}개가 초기 비밀번호 1234 로 만들어집니다.\n건물 번호는 나중에 바꿀 수 없습니다.`;
  if (!confirm(message)) return;
  const button = el('site-submit');
  button.disabled = true;
  button.textContent = '저장 중…';
  try {
    if (initialized) {
      const result = await post('/api/admin/site', { site }, 'PUT');
      toast(`설정을 저장했습니다 (v${result.site.version}). 호실 +${result.added} −${result.removed}`);
    } else {
      const result = await post('/api/admin/site/init', { site });
      toast(`초기 설정 완료 — 세대 계정 ${result.accounts.toLocaleString()}개를 만들었습니다.`);
    }
    await loadSite();
    openPage(initialized ? 'settings' : 'accounts');
  } catch (error) {
    const blocked = error.detail?.blocked;
    el('preview-body').insertAdjacentHTML('afterbegin', `<div class="alert alert-error">${safe(blocked ? blocked.join(' / ') : error.message)}</div>`);
    toast(error.message);
    button.disabled = false;
    button.textContent = initialized ? '설정 변경 저장' : '초기 설정 저장';
  }
}

// --- 세대 계정 -----------------------------------------------------------
const MODE_LABEL = { ROBOT: '로봇 배송', DIRECT: '직접 수령' };

function renderStats(target, items) {
  el(target).innerHTML = items.map(([label, value, sub]) => `<div class="stat"><span>${safe(label)}</span><strong>${safe(value)}</strong>${sub ? `<small>${safe(sub)}</small>` : ''}</div>`).join('');
}

async function loadAccounts() {
  const form = el('account-filter');
  const params = new URLSearchParams();
  if (form.building.value) params.set('building', form.building.value);
  if (form.q.value.trim()) params.set('q', form.q.value.trim());
  try {
    const data = await api(`/api/admin/accounts?${params}`);
    const select = el('account-building');
    select.innerHTML = data.buildings.map(b => `<option value="${b.code}" ${b.code === data.building ? 'selected' : ''}>${safe(b.name)} (${b.code})</option>`).join('');
    renderStats('account-stats', [
      ['활성 계정', data.stats.active.toLocaleString(), `전체 ${data.stats.total.toLocaleString()}`],
      ['초기 비밀번호 사용', data.stats.initial_password.toLocaleString(), '비밀번호를 아직 안 바꾼 세대'],
      ['이 건물 조회 결과', data.accounts.length.toLocaleString()]
    ]);
    el('account-table').innerHTML = `<thead><tr><th>계정</th><th>${safe(state.site.unit_term)}</th><th>상태</th><th>기본 수령</th><th>입주(초기화)</th><th>비밀번호 변경</th><th></th></tr></thead>
      <tbody>${data.accounts.map(a => `<tr class="${a.active ? '' : 'inactive'}">
        <td><code>${safe(a.user_id)}</code></td><td>${safe(a.label)}</td>
        <td>${a.active ? '' : '<span class="tag">비활성</span>'}${a.must_change ? '<span class="tag tag-warn">초기 비밀번호</span>' : ''}${a.locked ? '<span class="tag tag-danger">잠김</span>' : ''}${a.active && !a.must_change && !a.locked ? '<span class="tag tag-ok">정상</span>' : ''}</td>
        <td>${safe(MODE_LABEL[a.default_mode] || a.default_mode)}</td><td>${fmt(a.occupant_since)}</td><td>${fmt(a.password_changed_at)}</td>
        <td class="row-actions">${a.locked ? `<button class="btn btn-small" data-account-action="unlock" data-user="${safe(a.user_id)}" data-label="${safe(a.label)}">잠금 해제</button>` : ''}
          <button class="btn btn-small" data-account-action="reset" data-user="${safe(a.user_id)}" data-label="${safe(a.label)}" ${a.active ? '' : 'disabled'}>비밀번호 초기화</button>
          <button class="btn btn-small" data-account-action="occupant" data-user="${safe(a.user_id)}" data-label="${safe(a.label)}" ${a.active ? '' : 'disabled'}>입주자 변경</button></td>
      </tr>`).join('') || '<tr><td colspan="7" class="empty">조회된 계정이 없습니다.</td></tr>'}</tbody>`;
  } catch (error) { toast(error.message); }
}

const ACCOUNT_ACTIONS = {
  reset: {
    path: 'reset-password', title: '비밀번호 초기화', button: '1234 로 초기화',
    desc: '비밀번호를 잊은 세대의 비밀번호를 1234 로 되돌립니다. 그 세대의 로그인은 모두 끝나고, 다음 로그인 때 비밀번호를 바꾸라고 안내합니다. 택배 기록은 그대로입니다.',
    confirm: label => `${label} 의 비밀번호를 1234 로 초기화할까요?`,
    done: (label, r) => `${label} 비밀번호를 1234 로 초기화했습니다${r.revoked ? ` (로그인 ${r.revoked}개 종료)` : ''}.`
  },
  occupant: {
    path: 'change-occupant', title: '입주자 변경', button: '새 입주자로 초기화',
    desc: '이사로 세대가 바뀌었을 때 씁니다. 비밀번호 1234·기본 수령 방법 로봇 배송으로 되돌리고, 이전 입주자의 택배 목록과 알림은 새 입주자에게 보이지 않습니다. 보관 중인 물품은 그대로 남습니다.',
    confirm: label => `${label} 을(를) 새 입주자로 초기화할까요?\n이전 입주자의 기록은 세대 웹에서 보이지 않게 됩니다.`,
    done: label => `${label} 을(를) 새 입주자로 초기화했습니다.`
  },
  unlock: {
    path: 'unlock', confirm: label => `${label} 의 로그인 잠금을 풀까요?`, done: label => `${label} 잠금을 풀었습니다.`
  }
};

async function runAccountAction(kind, userId, label) {
  const action = ACCOUNT_ACTIONS[kind];
  if (!confirm(action.confirm(label))) return null;
  const result = await post(`/api/admin/accounts/${encodeURIComponent(userId)}/${action.path}`);
  const message = action.done(label, result);
  toast(message);
  return message;
}

function renderAccountAction(kind) {
  const action = ACCOUNT_ACTIONS[kind];
  const form = el('action-form');
  form.dataset.kind = kind;
  el('action-title').textContent = action.title;
  el('action-desc').textContent = action.desc;
  el('action-submit').textContent = action.button;
  form.building.innerHTML = state.publicSite.buildings.map(b => `<option value="${b.code}">${safe(b.name)} (${b.code})</option>`).join('');
  form.unit.value = '';
  showAlert('action-result');
  updateActionGuide();
}

function updateActionGuide() {
  const form = el('action-form');
  const building = state.publicSite.buildings.find(b => String(b.code) === form.building.value);
  const unit = Number(form.unit.value);
  const guide = el('action-guide');
  guide.classList.remove('error');
  guide.textContent = building?.guide || '';
  if (form.unit.value && building && !building.units.includes(unit)) {
    guide.textContent = `${building.name}에 없는 ${state.site.unit_term}입니다. ${building.guide}`;
    guide.classList.add('error');
  }
}

async function submitAccountAction(form) {
  const building = state.publicSite.buildings.find(b => String(b.code) === form.building.value);
  const unit = Number(form.unit.value);
  if (!building || !building.units.includes(unit)) {
    showAlert('action-result', `${state.site.unit_term}을 확인하세요.`);
    return;
  }
  try {
    const message = await runAccountAction(form.dataset.kind, `${building.code}-${unit}`, `${building.name} ${unit}${state.site.unit_term}`);
    if (message) { showAlert('action-result', message, 'ok'); form.unit.value = ''; }
  } catch (error) { showAlert('action-result', error.message); }
}

// --- 보관 현황 -----------------------------------------------------------
const STATUS_LABEL = { REGISTERED: '투입 대기', STORED: '보관 중', JOB_ASSIGNED: '배송 준비', LOADED: '로봇 적재' };
const PLAN_LABEL = { CHOICE_OPEN: '시간 선택 중', RESERVED: '예약', AUTO: '자동 배송' };
const LOCK_LABEL = { FRAGILE: '취급 주의', OVERSIZE: '규격 초과', FAILED: '배달 실패' };

async function loadStorage() {
  try {
    state.storage = await api('/api/admin/storage');
    const { occupancy: o, waiting, active_jobs: jobs } = state.storage;
    renderStats('storage-stats', [
      ['그리드 보관함', `${o.grid.used} / ${o.grid.total}`, `로봇 위 ${o.grid.on_robot ?? 0}개`],
      ['배송 불가 보관함', `${o.oversize.used} / ${o.oversize.total}`],
      ['로봇 배송 대기', waiting.ROBOT], ['직접 수령 대기', waiting.DIRECT],
      ['진행 중 배송', jobs.length, jobs.map(j => `${j.job_id} ${j.status}`).join(', ') || '없음']
    ]);
    el('demo-bar').hidden = !state.storage.demo;
    renderStorageTable();
  } catch (error) { if (error.status !== 401) toast(error.message); }
}

// 대회 시연 전용: 대기 시간 건너뛰기 (보관소 서버 LASTGATE_DEMO=1)
async function demoSkip() {
  const button = el('demo-skip');
  button.disabled = true;
  try {
    const res = await post('/api/admin/demo/skip-wait');
    showAlert('demo-alert', res.summary, res.jobs.length ? 'ok' : 'error');
    await loadStorage();
  } catch (error) {
    if (error.status !== 401) showAlert('demo-alert', error.message);
  } finally { button.disabled = false; }
}

function renderStorageTable() {
  if (!state.storage) return;
  const area = el('storage-area').value;
  const mode = el('storage-mode').value;
  const q = el('storage-q').value.trim();
  const items = state.storage.items.filter(it => (!area || it.storage_area === area) && (!mode || it.mode === mode)
    && (!q || it.label.includes(q) || it.receipt_no.includes(q) || String(it.unit).includes(q)));
  el('storage-table').innerHTML = `<thead><tr><th>위치</th><th>세대</th><th>상태</th><th>수령</th><th>배송</th><th>접수번호</th><th>보관 시각</th></tr></thead>
    <tbody>${items.map(it => `<tr>
      <td><code>${safe(it.space_id || '-')}</code></td><td>${safe(it.label)}</td>
      <td>${safe(STATUS_LABEL[it.status] || it.status)}${it.robot_id ? ` · ${safe(it.robot_id)}` : ''}</td>
      <td>${safe(MODE_LABEL[it.mode] || it.mode)}${it.direct_lock ? ` <span class="tag tag-warn">${safe(LOCK_LABEL[it.direct_lock])}</span>` : ''}</td>
      <td>${safe(PLAN_LABEL[it.plan] || '')}${it.slot_label ? ` ${safe(it.slot_label)}` : ''}${it.job_id ? ` <small>${safe(it.job_id)}</small>` : ''}</td>
      <td><small>${safe(it.receipt_no)}</small></td><td>${fmt(it.stored_at || it.registered_at)}</td></tr>`).join('')
      || '<tr><td colspan="7" class="empty">보관 중인 물품이 없습니다.</td></tr>'}</tbody>`;
}

// --- 코드로 수령 처리 ----------------------------------------------------
function spaceText(item) {
  return item.storage_area === 'OVERSIZE' ? `배송 불가 보관함 ${String(item.door).padStart(2, '0')}번 문` : `그리드 보관함 ${item.space_id}`;
}

async function lookupCode(form, confirmPickup = false) {
  const code = form.code.value.trim();
  const box = el('code-result');
  if (!/^\d{6}$/.test(code)) { box.hidden = false; box.innerHTML = '<p class="alert alert-error">인증코드 6자리를 입력하세요.</p>'; return; }
  try {
    const result = await post('/api/admin/code-pickup', { code, confirm: confirmPickup });
    const it = result.item;
    box.hidden = false;
    if (result.confirmed) {
      box.innerHTML = `<p class="alert alert-ok">${safe(it.label)} 택배를 수령 처리했습니다. <b>${safe(spaceText(it))}</b> 에서 꺼내 전달하세요.</p>`;
      form.reset();
      return;
    }
    box.innerHTML = `<dl class="kv"><div><dt>세대</dt><dd>${safe(it.label)}</dd></div><div><dt>위치</dt><dd>${safe(spaceText(it))}</dd></div>
      <div><dt>수령 방식</dt><dd>${safe(MODE_LABEL[it.mode])}${it.fragile ? ' · 취급 주의' : ''}</dd></div><div><dt>접수번호</dt><dd>${safe(it.receipt_no)}</dd></div></dl>
      <p class="hint">본인 확인 후 수령 처리하세요. 로봇 배송 예정이던 물품이면 배송에서 빠집니다.</p>
      <button class="btn btn-primary" type="button" id="code-confirm">수령 처리</button>`;
  } catch (error) {
    box.hidden = false;
    box.innerHTML = `<p class="alert alert-error">${safe(error.message)}</p>`;
  }
}

// --- 작업 기록 -----------------------------------------------------------
const EVENT_LABEL = {
  ADMIN_LOGIN: '관리자 로그인', ADMIN_PASSWORD: '관리자 비밀번호 변경', ADMIN_SITE_INIT: '초기 설정',
  ADMIN_SITE_UPDATE: '설정 변경', ADMIN_SITE_RESET: '전체 재설정', ADMIN_RESET_PASSWORD: '비밀번호 초기화',
  ADMIN_CHANGE_OCCUPANT: '입주자 변경', ADMIN_UNLOCK: '잠금 해제', ADMIN_CODE_PICKUP: '코드로 수령 처리',
  STORE: '보관', RETRIEVE: '수령', LOAD_ROBOT: '로봇 적재', RETURN: '보관소 복귀'
};

function eventDetail(data) {
  if (!data) return '';
  return Object.entries(data).filter(([key]) => key !== 'admin')
    .map(([key, value]) => `${key}: ${typeof value === 'object' ? JSON.stringify(value) : value}`).join(' · ');
}

async function loadEvents() {
  try {
    const data = await api(`/api/admin/events?limit=200${el('events-all').checked ? '&all=1' : ''}`);
    el('events-table').innerHTML = `<thead><tr><th>시각</th><th>작업</th><th>관리자</th><th>내용</th></tr></thead>
      <tbody>${data.events.map(e => `<tr><td>${fmt(e.at)}</td><td>${safe(EVENT_LABEL[e.kind] || e.kind)}</td>
        <td>${safe(e.data?.admin || '')}</td><td><small>${safe(eventDetail(e.data))}</small></td></tr>`).join('')
        || '<tr><td colspan="4" class="empty">기록이 없습니다.</td></tr>'}</tbody>`;
  } catch (error) { toast(error.message); }
}

// --- 전체 재설정·관리자 비밀번호 ------------------------------------------
async function submitReset(form) {
  showAlert('reset-alert');
  if (form.confirmName.value.trim() !== state.site.name) return showAlert('reset-alert', '사이트 이름이 일치하지 않습니다.');
  if (!confirm('정말 전체 재설정할까요? 현장 설정·세대 계정·물품 기록이 모두 지워집니다.')) return;
  try {
    await post('/api/admin/site/reset', { password: form.password.value, confirmName: form.confirmName.value.trim() });
    toast('전체 재설정을 마쳤습니다. 초기 설정부터 다시 하세요.');
    await loadSite();
    openPage('init');
  } catch (error) { showAlert('reset-alert', error.message); }
}

async function submitAdminPassword(form) {
  const { currentPassword, newPassword, confirmPassword } = Object.fromEntries(new FormData(form));
  showAlert('admin-password-alert');
  if (!/^(?=.*[A-Za-z])(?=.*\d).{8,64}$/.test(newPassword)) return showAlert('admin-password-alert', '새 비밀번호는 영문과 숫자를 섞어 8자 이상입니다.');
  if (newPassword !== confirmPassword) return showAlert('admin-password-alert', '새 비밀번호 확인이 일치하지 않습니다.');
  try {
    const result = await post('/api/admin/password', { currentPassword, newPassword }, 'PATCH');
    state.me.default_password = false;
    renderChrome();
    form.reset();
    toast(`관리자 비밀번호를 바꿨습니다${result.revoked ? ` (다른 로그인 ${result.revoked}개 종료)` : ''}.`);
    openPage(state.site ? 'storage' : 'init');
  } catch (error) { showAlert('admin-password-alert', error.message); }
}

// --- 연결 상태·자동 로그아웃 ---------------------------------------------
async function checkHealth() {
  try {
    const health = await (await fetch('/api/health')).json();
    const ok = health.locker === 'connected' && health.server !== false;
    el('conn').classList.toggle('offline', !ok);
    el('conn-text').textContent = health.locker !== 'connected' ? '브로커 연결 끊김' : health.server === false ? '보관소 서버 꺼짐' : '보관소 연결됨';
  } catch {
    el('conn').classList.add('offline');
    el('conn-text').textContent = '관리자 웹 서버 응답 없음';
  }
}

async function logout(message = '') {
  await api('/api/admin/logout', { method: 'POST' }).catch(() => {});
  showLogin(message);
}

['click', 'keydown', 'input'].forEach(type => document.addEventListener(type, () => { state.lastActivity = Date.now(); }, { passive: true }));
setInterval(() => {
  if (state.me && Date.now() - state.lastActivity > IDLE_LIMIT_MS) logout('30분 동안 사용하지 않아 로그아웃했습니다.');
}, 30000);
setInterval(checkHealth, 15000);

// --- 이벤트 -------------------------------------------------------------
document.addEventListener('click', async event => {
  const target = event.target;
  const pageButton = target.closest('[data-page]');
  if (pageButton) { openPage(pageButton.dataset.page); return; }
  if (target.closest('#logout-button')) { await logout(); return; }
  if (target.closest('#add-building')) {
    const codes = [...el('buildings').querySelectorAll('[data-field="code"]')].map(input => Number(input.value) || 0);
    addBuildingRow(blankBuilding(Math.min(999, Math.max(0, ...codes) + 1)));
    schedulePreview();
    return;
  }
  const remove = target.closest('[data-remove-building]');
  if (remove) { remove.closest('.building-row').remove(); updateRemoveButtons(); schedulePreview(); return; }
  if (target.closest('#site-revert')) { renderSiteForm(); return; }
  const accountAction = target.closest('[data-account-action]');
  if (accountAction) {
    try {
      if (await runAccountAction(accountAction.dataset.accountAction, accountAction.dataset.user, accountAction.dataset.label)) await loadAccounts();
    } catch (error) { toast(error.message); }
    return;
  }
  if (target.closest('#code-confirm')) { await lookupCode(el('code-form'), true); return; }
  if (target.closest('#events-reload')) loadEvents();
});

el('site-form').addEventListener('input', () => schedulePreview());
el('site-form').addEventListener('change', () => schedulePreview());
el('site-form').addEventListener('submit', event => { event.preventDefault(); submitSite(); });
el('login-form').addEventListener('submit', event => { event.preventDefault(); handleLogin(event.currentTarget); });
el('account-filter').addEventListener('submit', event => { event.preventDefault(); loadAccounts(); });
el('account-building').addEventListener('change', () => { el('account-filter').q.value = ''; loadAccounts(); });
el('action-form').addEventListener('input', updateActionGuide);
el('action-form').addEventListener('submit', event => { event.preventDefault(); submitAccountAction(event.currentTarget); });
el('code-form').addEventListener('submit', event => { event.preventDefault(); lookupCode(event.currentTarget); });
el('events-all').addEventListener('change', loadEvents);
['storage-area', 'storage-mode', 'storage-q'].forEach(id => el(id).addEventListener('input', renderStorageTable));
el('demo-skip').addEventListener('click', demoSkip);
el('reset-form').addEventListener('submit', event => { event.preventDefault(); submitReset(event.currentTarget); });
el('admin-password-form').addEventListener('submit', event => { event.preventDefault(); submitAdminPassword(event.currentTarget); });

async function boot() {
  checkHealth();
  try {
    await loadSite();
    await enterApp();
  } catch {
    showLogin();
  }
}
boot();
