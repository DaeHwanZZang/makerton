// LastGate 세대 웹 화면. 데이터는 모두 세대 웹 서버(/api)를 거쳐 보관소 서버에서 온다.
// 실시간: /api/user/events (SSE) — 끊겨도 60초마다 목록을 다시 불러온다.
const initialView = location.hash.slice(1) === 'robots' ? 'robots' : 'parcels';
const state = {
  config: null, user: null, parcels: [], robots: [], unread: 0, notifications: [],
  clockOffset: 0, loading: false, preferenceLoading: false, preferenceOpen: false, view: initialView,
  pickupCodes: new Map(), pickupCodeTimers: new Map(), pickupCodeLoading: new Set(),
  slot: null,          // 배송 시간 선택 창 상태 { slots, selectedIds:Set, start, seatsPerSlot }
  events: null, siteVersion: null, reloadPending: false
};
const el = id => document.getElementById(id);
const safe = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
const now = () => Date.now() + state.clockOffset;
const SEED_PASSWORD = '1234';

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers }
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body?.error?.message || '요청을 처리하지 못했습니다.');
    Object.assign(error, { status: response.status, code: body?.error?.code, detail: body?.error || {} });
    throw error;
  }
  return body;
}

function showAlert(id, message = '') {
  el(id).textContent = message;
  el(id).hidden = !message;
}

function setServiceState(online) {
  el('live-pill').classList.toggle('offline', !online);
  el('live-text').textContent = online ? '실시간 연결' : '보관소 연결 끊김';
}

let toastTimer;
function toast(message) {
  el('toast').textContent = message;
  el('toast').hidden = false;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { el('toast').hidden = true; }, 3200);
}

const time = iso => new Date(iso).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', hour12: false });
function dayLabel(iso) {
  const day = new Date(iso); day.setHours(0, 0, 0, 0);
  const today = new Date(now()); today.setHours(0, 0, 0, 0);
  const diff = Math.round((day - today) / 86400000);
  return diff === 0 ? '오늘' : diff === 1 ? '내일' : new Date(iso).toLocaleDateString('ko-KR', { month: 'long', day: 'numeric' });
}
const minutesLeft = iso => Math.max(0, Math.ceil((new Date(iso).getTime() - now()) / 60000));

// --- 로그인 화면 -----------------------------------------------------------
function site() { return state.config?.site || null; }
function selectedBuilding() {
  const code = Number(el('building-select').value);
  return site()?.buildings.find(b => b.code === code) || null;
}

function renderLoginForm() {
  const s = site();
  el('not-initialized').hidden = state.config?.initialized !== false;
  el('login-form').querySelector('button[type="submit"]').disabled = state.config?.initialized === false;
  if (!s) return;
  el('site-name').textContent = s.name;
  el('building-term').textContent = s.building_term;
  el('unit-term').textContent = s.unit_term;
  el('login-help').textContent = `${s.building_term}과 ${s.unit_term}, 비밀번호로 로그인하세요. 처음이면 비밀번호는 ${SEED_PASSWORD} 입니다.`;
  const select = el('building-select');
  const previous = select.value;
  select.innerHTML = s.buildings.map(b => `<option value="${b.code}">${safe(b.name)}</option>`).join('');
  if (s.buildings.some(b => String(b.code) === previous)) select.value = previous;
  el('building-field').hidden = s.buildings.length <= 1;
  renderUnitGuide();
}

function renderUnitGuide() {
  const building = selectedBuilding();
  const value = el('unit-input').value.trim();
  const hint = el('unit-guide');
  hint.classList.remove('error');
  if (!building) { hint.textContent = ''; return; }
  if (value && /^\d+$/.test(value) && !building.units.some(unit => String(unit).startsWith(value))) {
    hint.textContent = `${building.name}에 없는 ${site().unit_term}입니다. ${building.guide}`;
    hint.classList.add('error');
    return;
  }
  hint.textContent = building.guide;
}

async function handleLogin(form) {
  const data = Object.fromEntries(new FormData(form));
  if (el('building-field').hidden && site()) data.building = site().buildings[0]?.code;
  showAlert('auth-alert');
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const result = await api('/api/auth/login', { method: 'POST', body: JSON.stringify(data) });
    state.user = result.user;
    form.reset();
    renderLoginForm();
    await enterDashboard();
  } catch (error) {
    const extra = error.detail?.remaining !== undefined && error.detail.remaining > 0
      ? ` (남은 시도 ${error.detail.remaining}번)` : '';
    showAlert('auth-alert', error.message + extra);
  } finally {
    button.disabled = state.config?.initialized === false;
  }
}

// --- 택배 카드 -------------------------------------------------------------
function tags(parcel) {
  const out = [];
  if (parcel.fragile) out.push('<span class="tag tag-warn">취급 주의</span>');
  if (parcel.storageArea === 'OVERSIZE') out.push(`<span class="tag tag-info">배송 불가 보관함 ${String(parcel.door ?? '').padStart(2, '0')}번</span>`);
  if (parcel.directLock === 'FAILED') out.push('<span class="tag tag-danger">배달 2회 실패</span>');
  return out.join('');
}

function pickupBlock(parcel) {
  const code = state.pickupCodes.get(parcel.id);
  const loading = state.pickupCodeLoading.has(parcel.id);
  const where = parcel.storageArea === 'OVERSIZE'
    ? `출력 키오스크에서 로그인하면 배송 불가 보관함 ${String(parcel.door ?? '').padStart(2, '0')}번 문이 열려요.`
    : '출력 키오스크에서 로그인하면 보관함이 올라와요.';
  return `<div class="pickup-copy"><strong>${safe(parcel.lockLabel || '직접 수령 대기')}</strong><span>${where}</span>
    <small>인증코드는 관리실에서 대신 찾을 때 쓰는 보조 수단입니다.</small></div>
    <div class="pickup-code-actions">
      <button class="ghost-button" type="button" data-${code ? 'hide' : 'reveal'}-code="${safe(parcel.id)}" ${loading ? 'disabled' : ''}>
        ${code ? '인증코드 숨기기' : loading ? '불러오는 중…' : '인증코드 보기'}</button>
      ${code ? `<div class="pickup-code-block"><small>인증코드</small><strong class="pickup-code">${safe(code)}</strong></div>` : ''}
    </div>`;
}

function parcelBody(parcel) {
  const id = safe(parcel.id);
  if (parcel.status === 'IN_TRANSIT') {
    return `<div class="state-box state-moving"><strong>로봇 배송 중${parcel.robotId ? ` · ${safe(parcel.robotId)}` : ''}</strong>
      <span>곧 문 앞에 도착해요.</span><button class="text-link" type="button" data-open-view="robots">로봇 위치 보기 →</button></div>`;
  }
  if (parcel.status === 'DELIVERED' || parcel.status === 'PICKED_UP') {
    return `<div class="state-box state-done"><span>${parcel.status === 'DELIVERED' ? '로봇 배송이 끝났어요.' : '직접 수령한 택배예요.'}</span>
      <button class="text-link danger" type="button" data-remove-completed="${id}">목록에서 삭제</button></div>`;
  }
  if (parcel.deliveryMethod === 'PICKUP') {
    const toRobot = parcel.canChooseDelivery && !parcel.directLock
      ? `<button class="ghost-button" type="button" data-set-delivery="ROBOT" data-parcel="${id}" ${state.user.defaultDelivery === 'PICKUP' ? 'disabled title="기본 수령 방법이 직접 수령이면 로봇 배송을 고를 수 없어요."' : ''}>로봇 배송으로 바꾸기</button>`
      : '';
    return `<div class="state-box state-pickup">${pickupBlock(parcel)}</div>${toRobot ? `<div class="card-actions">${toRobot}</div>` : ''}`;
  }
  // 로봇 배송
  if (parcel.dispatched) {
    return `<div class="state-box state-fixed"><strong>로봇 배송 준비 중</strong>
      <span>${parcel.slot ? `예약 시간 ${safe(parcel.slot.label)} 배송을 위해 ` : ''}로봇이 보관소에서 택배를 싣고 있어요.</span>
      <small>로봇에 실리기 전까지는 출력 키오스크에서 직접 꺼낼 수도 있어요.</small></div>`;
  }
  const toPickup = parcel.canChooseDelivery
    ? `<button class="ghost-button" type="button" data-set-delivery="PICKUP" data-parcel="${id}">직접 수령으로 바꾸기</button>` : '';
  if (parcel.plan === 'RESERVED' && parcel.slot) {
    const editable = parcel.reservable;
    return `<div class="state-box state-reserved"><strong>예약됨 ${safe(dayLabel(parcel.slot.start))} ${safe(parcel.slot.label)}</strong>
      <span>${editable ? `${time(parcel.slot.changeUntil)}까지 바꾸거나 취소할 수 있어요.` : '변경 마감 시간이 지나 예약대로 배송돼요.'}</span></div>
      <div class="card-actions">${editable ? `<button class="ghost-button" type="button" data-open-slots="${id}">변경</button>
      <button class="ghost-button" type="button" data-cancel-reservation="${id}">예약 취소</button>` : ''}${editable ? toPickup : ''}</div>`;
  }
  if (parcel.plan === 'CHOICE_OPEN' && parcel.choiceDeadline) {
    const left = minutesLeft(parcel.choiceDeadline);
    return `<div class="state-box state-choice"><strong>배송 시간 선택 가능 · ${left}분 남음</strong>
      <span>${time(parcel.choiceDeadline)}까지 고르지 않으면 자동 배송으로 확정돼요.</span></div>
      <div class="card-actions"><button class="primary-button small" type="button" data-open-slots="${id}">배송 시간 고르기</button>${toPickup}</div>`;
  }
  return `<div class="state-box state-fixed"><strong>자동 배송 확정</strong>
    <span>같은 건물 택배와 묶어 순서대로 배송돼요. 로봇이 출발하면 알려드릴게요.</span>
    <small>로봇에 실리기 전까지는 출력 키오스크에서 직접 꺼낼 수도 있어요.</small></div>`;
}

function statusClass(parcel) {
  if (parcel.status !== 'STORED') return parcel.status;
  if (parcel.deliveryMethod === 'PICKUP') return 'PICKUP';
  return parcel.plan || 'STORED';
}

function renderParcels() {
  const list = el('parcel-grid');
  if (!state.parcels.length) {
    list.innerHTML = '<div class="empty-card"><span class="empty-icon" aria-hidden="true">□</span><strong>보관 중인 택배가 없어요</strong><p>택배가 보관소에 도착하면 알림으로 알려드려요.</p></div>';
    return;
  }
  list.innerHTML = state.parcels.map(parcel => `<article class="parcel-card">
    <div class="parcel-top"><div><span class="parcel-id">${safe(parcel.id)}</span>
      <h3>${safe(parcel.trackingNumber || '송장번호 미등록')}</h3>
      <div class="tags">${tags(parcel)}</div></div>
      <span class="status-pill status-${safe(statusClass(parcel))}">${safe(parcel.statusLabel)}</span></div>
    <div class="parcel-meta"><span>수령 방법 <b>${parcel.deliveryMethod === 'PICKUP' ? '직접 수령' : '로봇 배송'}</b></span>
      <span>도착 <b>${parcel.createdAt ? `${dayLabel(parcel.createdAt)} ${time(parcel.createdAt)}` : '-'}</b></span></div>
    ${parcelBody(parcel)}</article>`).join('');
}

function renderSummary() {
  const active = state.parcels.filter(p => p.status === 'STORED');
  const choice = active.filter(p => p.plan === 'CHOICE_OPEN' && p.deliveryMethod === 'ROBOT').length;
  const pickup = active.filter(p => p.deliveryMethod === 'PICKUP').length;
  const robot = state.parcels.filter(p => p.status === 'IN_TRANSIT' || (p.status === 'STORED' && p.deliveryMethod === 'ROBOT')).length;
  el('summary-grid').innerHTML = [
    ['보관 중', active.length, 'highlight'], ['시간 선택 가능', choice, choice ? 'attention' : ''],
    ['직접 수령 대기', pickup, ''], ['로봇 배송 예정·중', robot, '']
  ].map(([label, value, cls]) => `<div class="summary-card ${cls}"><span>${label}</span><strong>${value}</strong></div>`).join('');
  el('parcel-nav-count').textContent = active.length + state.parcels.filter(p => p.status === 'IN_TRANSIT').length;
  el('hero-text').textContent = choice
    ? `배송 시간을 고를 수 있는 택배가 ${choice}개 있어요.`
    : active.length ? `보관 중인 택배 ${active.length}개` : '도착한 택배가 생기면 알려드려요.';
}

function renderRobots() {
  const moving = state.parcels.filter(p => p.status === 'IN_TRANSIT').length;
  el('robot-nav-count').hidden = !state.robots.length;
  el('robot-nav-count').textContent = state.robots.length;
  el('robot-grid').innerHTML = state.robots.length ? state.robots.map(robot => `<article class="robot-card ${robot.connected ? '' : 'offline'}">
    <div><span class="robot-state"><i></i>${robot.connected ? '통신 정상' : '통신 끊김'}</span><h3>${safe(robot.id)}</h3>
      <p>${safe(robot.status)}</p><p>현재 위치 <strong>${safe(robot.location)}</strong></p>
      <p class="muted">싣고 있는 내 택배 ${robot.parcelIds.length}개</p></div>
    <div class="robot-battery">${robot.battery === null ? '-' : `${safe(robot.battery)}%`}</div></article>`).join('')
    : `<div class="empty-card"><strong>${moving ? '로봇 상태를 받는 중이에요' : '내 택배를 싣고 있는 로봇이 없어요'}</strong>
      <p>로봇이 내 택배를 싣고 출발하면 여기에서 위치를 볼 수 있어요.</p></div>`;
}

function renderDeliveryPreference() {
  const method = state.user?.defaultDelivery === 'PICKUP' ? 'PICKUP' : 'ROBOT';
  el('default-delivery-label').textContent = method === 'ROBOT' ? '로봇 배송' : '직접 수령';
  el('preference-toggle').setAttribute('aria-expanded', String(state.preferenceOpen));
  el('delivery-preference-panel').hidden = !state.preferenceOpen;
  document.querySelectorAll('[data-default-delivery]').forEach(button => {
    const active = button.dataset.defaultDelivery === method;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
    button.disabled = state.preferenceLoading;
  });
}

function renderUser() {
  el('user-name').textContent = `${state.user.name} 택배`;
  el('must-change-banner').hidden = !state.user.mustChange;
  const s = site();
  el('account-info').innerHTML = `
    <div><dt>${safe(s?.building_term || '건물')}</dt><dd>${safe(state.user.buildingName)}</dd></div>
    <div><dt>${safe(s?.unit_term || '호실')}</dt><dd>${safe(state.user.unit)}</dd></div>
    <div><dt>계정</dt><dd>${safe(state.user.userId)}</dd></div>`;
}

function renderUnread() {
  el('unread-badge').hidden = !state.unread;
  el('unread-badge').textContent = state.unread > 99 ? '99+' : state.unread;
}

function renderDashboard() {
  renderUser();
  renderSummary();
  renderDeliveryPreference();
  renderParcels();
  renderRobots();
  renderUnread();
  setDashboardView(state.view, { updateHash: false, scroll: false });
}

function setDashboardView(view, { updateHash = true, scroll = true } = {}) {
  const next = view === 'robots' ? 'robots' : 'parcels';
  state.view = next;
  el('parcels-view').hidden = next !== 'parcels';
  el('robots-view').hidden = next !== 'robots';
  document.querySelectorAll('[data-dashboard-view]').forEach(button => {
    button.classList.toggle('active', button.dataset.dashboardView === next);
    button.setAttribute('aria-current', button.dataset.dashboardView === next ? 'page' : 'false');
  });
  if (updateHash) history.replaceState(null, '', next === 'parcels' ? location.pathname : `#${next}`);
  if (scroll) window.scrollTo({ top: 0, behavior: 'smooth' });
  if (next === 'robots') loadRobots();
}

// --- 데이터 불러오기 -------------------------------------------------------
function dropStalePickupCodes() {
  for (const id of state.pickupCodes.keys()) {
    if (!state.parcels.some(p => p.id === id && p.status === 'STORED' && p.deliveryMethod === 'PICKUP')) {
      window.clearTimeout(state.pickupCodeTimers.get(id));
      state.pickupCodeTimers.delete(id);
      state.pickupCodes.delete(id);
    }
  }
}

function handleApiError(error, quiet) {
  if (error.status === 401) { showAuth(); return; }
  if (error.status === 503) setServiceState(false);
  if (!quiet) toast(error.message);
}

async function loadDashboard({ quiet = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  try {
    const data = await api('/api/user/parcels');
    state.user = data.user;
    state.parcels = data.parcels;
    state.unread = data.unread;
    if (data.serverTime) state.clockOffset = new Date(data.serverTime).getTime() - Date.now();
    dropStalePickupCodes();
    setServiceState(true);
    renderDashboard();
    if (state.parcels.some(p => p.status === 'IN_TRANSIT') || state.view === 'robots') loadRobots();
    else { state.robots = []; renderRobots(); }
    if (!quiet) toast('최신 상태로 불러왔어요.');
  } catch (error) {
    handleApiError(error, quiet);
  } finally {
    state.loading = false;
  }
}

async function loadRobots() {
  try {
    state.robots = (await api('/api/user/robots')).robots;
    renderRobots();
  } catch (error) { handleApiError(error, true); }
}

async function loadConfig() {
  try {
    state.config = await api('/api/config');
    if (state.siteVersion === null) state.siteVersion = state.config.version;
  } catch { state.config = null; }
  renderLoginForm();
}

// --- 실시간 (SSE) ---------------------------------------------------------
function connectEvents() {
  closeEvents();
  const source = new EventSource('/api/user/events');
  state.events = source;
  source.addEventListener('ready', () => setServiceState(true));
  source.addEventListener('parcels', () => loadDashboard({ quiet: true }));
  source.addEventListener('notification', event => {
    const notice = JSON.parse(event.data);
    toast(notice.title + (notice.body ? ` — ${notice.body}` : ''));
    state.unread += 1;
    renderUnread();
    if (el('notice-dialog').open) loadNotifications();
    loadDashboard({ quiet: true });
  });
  source.addEventListener('slots', event => applySlotUpdate(JSON.parse(event.data).slots || []));
  source.addEventListener('config', event => {
    const { version } = JSON.parse(event.data);
    if (state.siteVersion !== null && version !== state.siteVersion) scheduleReload();
  });
  source.addEventListener('server', event => setServiceState(JSON.parse(event.data).online));
  source.onerror = () => {
    setServiceState(false);
    // 세션이 끝났으면(401) 재연결을 멈춘다. 확인은 다음 목록 갱신이 한다.
    if (source.readyState === EventSource.CLOSED) window.setTimeout(() => state.user && connectEvents(), 10000);
  };
}

function closeEvents() {
  state.events?.close();
  state.events = null;
}

// 현장 설정 버전이 바뀌면, 사용자가 창을 열어 두지 않은 한가한 때에 새로 불러온다.
function scheduleReload() {
  state.reloadPending = true;
  el('reload-banner').hidden = false;
  reloadIfIdle();
}
function reloadIfIdle() {
  if (!state.reloadPending) return;
  const busy = document.querySelector('dialog[open]') || document.activeElement?.matches('input, select');
  if (!busy) location.reload();
}

// --- 배송 시간 예약 -------------------------------------------------------
async function openSlots(parcelId) {
  const parcel = state.parcels.find(p => p.id === parcelId);
  if (!parcel) return;
  state.slot = { slots: [], selectedIds: new Set([parcelId]), start: parcel.slot?.start || null, current: parcel.slot?.start || null, seatsPerSlot: 2 };
  showAlert('slot-alert');
  renderSlotParcels();
  el('slot-list').innerHTML = '<p class="muted">예약 가능한 시간을 불러오는 중…</p>';
  if (!el('slot-dialog').open) el('slot-dialog').showModal();
  await loadSlots();
}

async function loadSlots() {
  try {
    const data = await api('/api/user/slots');
    if (!state.slot) return;
    state.slot.slots = data.slots;
    state.slot.seatsPerSlot = data.seatsPerSlot;
    el('slot-help').textContent = `10분 단위로 한 칸에 ${data.seatsPerSlot}자리까지 먼저 예약한 순서대로 잡혀요. `
      + `배송 시간대 ${data.serviceStart}~${data.serviceEnd} · 배송 20분 전까지 바꾸거나 취소할 수 있어요.`;
    renderSlots();
  } catch (error) {
    if (error.status === 401) { el('slot-dialog').close(); showAuth(); return; }
    showAlert('slot-alert', error.message);
  }
}

function reservableParcels() {
  return state.parcels.filter(p => p.reservable);
}

function renderSlotParcels() {
  const options = reservableParcels();
  const box = el('slot-parcels');
  box.hidden = options.length <= 1;
  box.innerHTML = '<legend>함께 예약할 택배 (최대 2개)</legend>' + options.map(p => {
    const checked = state.slot.selectedIds.has(p.id);
    const full = !checked && state.slot.selectedIds.size >= 2;
    return `<label class="check-row"><input type="checkbox" data-slot-parcel="${safe(p.id)}" ${checked ? 'checked' : ''} ${full ? 'disabled' : ''}>
      <span>${safe(p.trackingNumber || p.id)}</span><small>${p.slot ? `현재 ${safe(p.slot.label)}` : safe(p.statusLabel)}</small></label>`;
  }).join('');
}

// 이 칸에 선택한 택배를 모두 넣을 수 있는가. 이미 이 칸에 예약된 내 택배 자리는 다시 쓴다.
function seatsNeeded(slot) {
  const reused = [...state.slot.selectedIds].filter(id => slot.mineParcelIds.includes(id)).length;
  return state.slot.selectedIds.size - reused;
}

function seatDots(slot) {
  const total = state.slot.seatsPerSlot;
  const mine = slot.mine;
  const others = total - slot.seatsLeft - mine;
  const dots = [...Array(mine).fill('mine'), ...Array(Math.max(0, others)).fill('taken'), ...Array(slot.seatsLeft).fill('free')];
  return dots.map(kind => `<i class="seat ${kind}"></i>`).join('');
}

function renderSlots() {
  const { slots } = state.slot;
  if (!slots.length) {
    el('slot-list').innerHTML = '<p class="muted">지금은 예약할 수 있는 시간이 없어요. 배송 시간대가 끝났거나 곧 시작해요.</p>';
    updateSlotFooter();
    return;
  }
  let lastDay = '';
  el('slot-list').innerHTML = slots.map(slot => {
    const day = dayLabel(slot.start);
    const header = day !== lastDay ? `<h4 class="slot-day">${safe(day)}</h4>` : '';
    lastDay = day;
    const need = seatsNeeded(slot);
    const enough = need <= (slot.restricted ? 0 : slot.seatsLeft);
    const selected = state.slot.start === slot.start;
    const closed = slot.seatsLeft === 0 && !slot.mine;
    return `${header}<button type="button" class="slot ${selected ? 'selected' : ''} ${slot.mine ? 'has-mine' : ''} ${closed ? 'closed' : ''}"
      data-slot="${safe(slot.start)}" ${enough || selected ? '' : 'disabled'} aria-pressed="${selected}">
      <span class="slot-time">${safe(slot.label.split('~')[0])}</span>
      <span class="slot-seats">${closed ? '<em>마감</em>' : seatDots(slot)}</span>
      ${slot.restricted ? '<small>같은 건물만</small>' : slot.mine ? '<small>내 예약</small>' : ''}</button>`;
  }).join('');
  updateSlotFooter();
}

function updateSlotFooter() {
  const slot = state.slot.slots.find(s => s.start === state.slot.start);
  const count = state.slot.selectedIds.size;
  const unchanged = slot && state.slot.current === slot.start && [...state.slot.selectedIds].every(id => slot.mineParcelIds.includes(id));
  el('slot-choice').textContent = slot
    ? `${dayLabel(slot.start)} ${slot.label} · 택배 ${count}개`
    : count ? `택배 ${count}개 · 시간을 고르세요.` : '예약할 택배를 고르세요.';
  el('slot-submit').disabled = !slot || !count || unchanged || seatsNeeded(slot) > slot.seatsLeft;
  el('slot-submit').textContent = state.slot.current ? '예약 변경' : '예약하기';
}

// 다른 세대 예약으로 남은 자리가 바뀌면 바로 반영한다.
function applySlotUpdate(updates) {
  if (!state.slot || !el('slot-dialog').open) return;
  let lost = false;
  for (const update of updates) {
    const slot = state.slot.slots.find(s => s.start === update.start);
    if (!slot) continue;
    const before = slot.seatsLeft;
    slot.seatsLeft = update.seatsLeft;
    if (!slot.restricted) slot.available = update.seatsLeft;
    if (slot.start === state.slot.start && update.seatsLeft < before && seatsNeeded(slot) > update.seatsLeft) lost = true;
  }
  if (lost) {
    state.slot.start = null;
    showAlert('slot-alert', '방금 다른 세대가 예약했어요. 다른 시간을 골라 주세요.');
  }
  renderSlots();
}

async function submitReservation() {
  const { start, selectedIds } = state.slot;
  el('slot-submit').disabled = true;
  try {
    const result = await api('/api/user/reservations', {
      method: 'POST', body: JSON.stringify({ slotStart: start, parcelIds: [...selectedIds] })
    });
    el('slot-dialog').close();
    state.parcels = result.parcels;
    renderDashboard();
    toast(`${dayLabel(result.slot.start)} ${result.slot.label} 배송으로 예약했어요.`);
  } catch (error) {
    if (error.status === 401) { el('slot-dialog').close(); showAuth(); return; }
    if (error.code === 'SLOT_FULL') {
      showAlert('slot-alert', error.detail.seatsLeft
        ? `방금 다른 세대가 예약했어요. 이 시간은 ${error.detail.seatsLeft}자리만 남았어요.`
        : '방금 다른 세대가 예약했어요. 다른 시간을 골라 주세요.');
      state.slot.start = null;
      await loadSlots();
      return;
    }
    showAlert('slot-alert', error.message);
    if (['SLOT_UNAVAILABLE', 'TOO_LATE', 'DELIVERY_FIXED'].includes(error.code)) { await loadSlots(); loadDashboard({ quiet: true }); }
    updateSlotFooter();
  }
}

async function cancelReservation(parcelId) {
  if (!window.confirm('배송 예약을 취소할까요? 시간 선택 기간이 지났으면 자동 배송으로 확정돼요.')) return;
  try {
    const result = await api(`/api/user/reservations/${encodeURIComponent(parcelId)}`, { method: 'DELETE' });
    state.parcels = result.parcels;
    renderDashboard();
    toast(result.plan === 'AUTO' ? '예약을 취소했어요. 자동 배송으로 확정됐어요.' : '예약을 취소했어요.');
  } catch (error) { handleApiError(error, false); loadDashboard({ quiet: true }); }
}

// --- 수령 방법 -----------------------------------------------------------
async function saveDeliveryPreference(method) {
  if (state.preferenceLoading || method === state.user.defaultDelivery) return;
  state.preferenceLoading = true;
  renderDeliveryPreference();
  try {
    const { preference } = await api('/api/user/preferences', { method: 'PATCH', body: JSON.stringify({ defaultDelivery: method }) });
    state.user.defaultDelivery = preference.defaultDelivery;
    state.parcels = preference.parcels;
    renderDashboard();
    const label = method === 'ROBOT' ? '로봇 배송' : '직접 수령';
    toast(`기본 수령 방법을 ${label}(으)로 바꿨어요.`
      + (preference.changed ? ` 택배 ${preference.changed}개도 함께 바꿨어요.` : '')
      + (preference.skipped ? ` ${preference.skipped}개는 직접 수령 전용이거나 배송이 확정돼 그대로예요.` : ''));
  } catch (error) { handleApiError(error, false); }
  finally {
    state.preferenceLoading = false;
    renderDeliveryPreference();
  }
}

async function setDelivery(parcelId, method) {
  try {
    await api(`/api/user/parcels/${encodeURIComponent(parcelId)}/delivery`, { method: 'PATCH', body: JSON.stringify({ method }) });
    await loadDashboard({ quiet: true });
    toast(method === 'ROBOT' ? '로봇 배송으로 바꿨어요. 배송 시간을 고를 수 있어요.' : '직접 수령으로 바꿨어요.');
  } catch (error) { handleApiError(error, false); loadDashboard({ quiet: true }); }
}

// --- 알림 ---------------------------------------------------------------
async function loadNotifications() {
  try {
    const data = await api('/api/user/notifications');
    state.notifications = data.notifications;
    state.unread = data.unread;
    renderUnread();
    el('notice-list').innerHTML = data.notifications.length ? data.notifications.map(n => `<article class="notice ${n.read ? '' : 'unread'}" data-notice="${n.id}">
      <strong>${safe(n.title)}</strong><p>${safe(n.body)}</p><small>${safe(dayLabel(n.createdAt))} ${safe(time(n.createdAt))}</small></article>`).join('')
      : '<p class="muted">아직 알림이 없어요.</p>';
  } catch (error) { handleApiError(error, false); }
}

async function markRead(body) {
  try {
    const data = await api('/api/user/notifications/read', { method: 'POST', body: JSON.stringify(body) });
    state.unread = data.unread;
    renderUnread();
    await loadNotifications();
  } catch (error) { handleApiError(error, false); }
}

// --- 설정 ---------------------------------------------------------------
async function handlePassword(form) {
  const data = Object.fromEntries(new FormData(form));
  showAlert('password-alert');
  if (!/^\d{4,8}$/.test(data.newPassword)) return showAlert('password-alert', '새 비밀번호는 숫자 4~8자리로 입력해 주세요.');
  if (data.newPassword === SEED_PASSWORD) return showAlert('password-alert', `초기 비밀번호(${SEED_PASSWORD})는 쓸 수 없어요.`);
  if (data.newPassword === data.currentPassword) return showAlert('password-alert', '현재 비밀번호와 다른 번호를 입력해 주세요.');
  if (data.newPassword !== data.confirmPassword) return showAlert('password-alert', '새 비밀번호 확인이 일치하지 않아요.');
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const result = await api('/api/user/password', {
      method: 'PATCH', body: JSON.stringify({ currentPassword: data.currentPassword, newPassword: data.newPassword })
    });
    state.user = result.user;
    renderUser();
    form.reset();
    el('settings-dialog').close();
    toast(result.revoked ? `비밀번호를 바꿨어요. 다른 기기 로그인 ${result.revoked}개를 끝냈어요.` : '비밀번호를 바꿨어요.');
  } catch (error) {
    if (error.status === 401) { el('settings-dialog').close(); showAuth(); return; }
    showAlert('password-alert', error.message);
  } finally { button.disabled = false; }
}

// --- 화면 전환 -----------------------------------------------------------
async function enterDashboard() {
  el('auth-view').hidden = true;
  el('dashboard-view').hidden = false;
  el('header-actions').hidden = false;
  await loadDashboard({ quiet: true });
  if (state.user) connectEvents();
}

function clearPickupCodes() {
  state.pickupCodeTimers.forEach(timer => window.clearTimeout(timer));
  state.pickupCodeTimers.clear();
  state.pickupCodes.clear();
  state.pickupCodeLoading.clear();
}

function showAuth() {
  closeEvents();
  clearPickupCodes();
  document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
  Object.assign(state, { user: null, parcels: [], robots: [], unread: 0, preferenceOpen: false, slot: null });
  el('parcel-grid').innerHTML = '';
  el('auth-view').hidden = false;
  el('dashboard-view').hidden = true;
  el('header-actions').hidden = true;
  loadConfig();
}

// --- 이벤트 -------------------------------------------------------------
document.addEventListener('click', async event => {
  const target = event.target;
  const nav = target.closest('[data-dashboard-view]') || target.closest('[data-open-view]');
  if (nav) { setDashboardView(nav.dataset.dashboardView || nav.dataset.openView); return; }
  if (target.closest('#refresh-button')) { await loadDashboard(); return; }
  if (target.closest('#reload-button')) { location.reload(); return; }
  if (target.closest('#bell-button')) { el('notice-dialog').showModal(); await loadNotifications(); return; }
  if (target.closest('#settings-button') || target.closest('[data-open-settings]')) {
    showAlert('password-alert');
    el('settings-dialog').showModal();
    return;
  }
  if (target.closest('[data-close-dialog]')) { target.closest('dialog').close(); return; }
  if (target.closest('#read-all-button')) { await markRead({ all: true }); return; }
  const notice = target.closest('[data-notice].unread');
  if (notice) { await markRead({ ids: [Number(notice.dataset.notice)] }); return; }
  if (target.closest('#logout-button')) {
    await api('/api/auth/logout', { method: 'POST' }).catch(() => {});
    showAuth();
    showAlert('auth-alert');
    return;
  }
  const openSlotButton = target.closest('[data-open-slots]');
  if (openSlotButton) { await openSlots(openSlotButton.dataset.openSlots); return; }
  const slotButton = target.closest('[data-slot]');
  if (slotButton && state.slot) {
    state.slot.start = slotButton.dataset.slot;
    showAlert('slot-alert');
    renderSlots();
    return;
  }
  if (target.closest('#slot-submit')) { await submitReservation(); return; }
  const cancelButton = target.closest('[data-cancel-reservation]');
  if (cancelButton) { await cancelReservation(cancelButton.dataset.cancelReservation); return; }
  const deliveryButton = target.closest('[data-set-delivery]');
  if (deliveryButton) { deliveryButton.disabled = true; await setDelivery(deliveryButton.dataset.parcel, deliveryButton.dataset.setDelivery); return; }
  const reveal = target.closest('[data-reveal-code]');
  if (reveal) {
    const id = reveal.dataset.revealCode;
    if (state.pickupCodeLoading.has(id)) return;
    const owner = state.user?.userId;
    state.pickupCodeLoading.add(id);
    renderParcels();
    try {
      const result = await api(`/api/user/parcels/${encodeURIComponent(id)}/pickup-code`, { cache: 'no-store' });
      if (owner !== state.user?.userId) return;
      state.pickupCodes.set(id, result.code);
      window.clearTimeout(state.pickupCodeTimers.get(id));
      state.pickupCodeTimers.set(id, window.setTimeout(() => {   // 60초 뒤 자동으로 가린다
        state.pickupCodes.delete(id);
        state.pickupCodeTimers.delete(id);
        renderParcels();
      }, 60000));
    } catch (error) { handleApiError(error, false); }
    finally { state.pickupCodeLoading.delete(id); renderParcels(); }
    return;
  }
  const hide = target.closest('[data-hide-code]');
  if (hide) {
    const id = hide.dataset.hideCode;
    window.clearTimeout(state.pickupCodeTimers.get(id));
    state.pickupCodeTimers.delete(id);
    state.pickupCodes.delete(id);
    renderParcels();
    return;
  }
  const remove = target.closest('[data-remove-completed]');
  if (remove) {
    if (!window.confirm('완료된 택배를 내 목록에서 삭제할까요?')) return;
    remove.disabled = true;
    try {
      await api(`/api/user/parcels/${encodeURIComponent(remove.dataset.removeCompleted)}`, { method: 'DELETE' });
      await loadDashboard({ quiet: true });
      toast('목록에서 삭제했어요.');
    } catch (error) { remove.disabled = false; handleApiError(error, false); }
    return;
  }
  if (target.closest('#preference-toggle')) {
    state.preferenceOpen = !state.preferenceOpen;
    renderDeliveryPreference();
    return;
  }
  const preference = target.closest('[data-default-delivery]');
  if (preference) await saveDeliveryPreference(preference.dataset.defaultDelivery);
});

document.addEventListener('change', event => {
  const box = event.target.closest('[data-slot-parcel]');
  if (box && state.slot) {
    if (box.checked) state.slot.selectedIds.add(box.dataset.slotParcel);
    else state.slot.selectedIds.delete(box.dataset.slotParcel);
    renderSlotParcels();
    renderSlots();
    return;
  }
  if (event.target.id === 'building-select') renderUnitGuide();
});

el('unit-input').addEventListener('input', event => {
  event.target.value = event.target.value.replace(/\D/g, '');
  renderUnitGuide();
});
el('login-form').addEventListener('submit', event => { event.preventDefault(); handleLogin(event.currentTarget); });
el('password-form').addEventListener('submit', event => { event.preventDefault(); handlePassword(event.currentTarget); });
document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('close', () => {
  if (dialog.id === 'slot-dialog') state.slot = null;
  window.setTimeout(reloadIfIdle, 0);
}));
document.addEventListener('focusout', () => window.setTimeout(reloadIfIdle, 0));
window.addEventListener('hashchange', () => setDashboardView(location.hash.slice(1), { updateHash: false }));

// SSE 가 끊겨도 놓치지 않도록 60초마다 다시 불러오고, 남은 시간 표시는 30초마다 갱신한다.
window.setInterval(() => { if (state.user) loadDashboard({ quiet: true }); }, 60000);
window.setInterval(() => { if (state.user && !state.loading) { renderParcels(); renderSummary(); } }, 30000);
window.setInterval(() => { if (state.user && state.view === 'robots') loadRobots(); }, 5000);

async function boot() {
  await loadConfig();
  try {
    state.user = (await api('/api/me')).user;
    await enterDashboard();
  } catch {
    showAuth();
  }
}
boot();
