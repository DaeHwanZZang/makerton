const initialView = ['parcels', 'robots'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview';
const state = { user: null, parcels: [], robots: [], loading: false, preferenceLoading: false, preferenceOpen: false, defaultDelivery: 'ROBOT', view: initialView, selectedParcelIds: new Set(), pickupCodes: new Map(), pickupCodeTimers: new Map(), pickupCodeLoading: new Set() };
const el = id => document.getElementById(id);
const safe = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers }
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body?.error?.message || '요청을 처리하지 못했습니다.');
    error.status = response.status;
    throw error;
  }
  return body;
}

function authError(message = '') {
  el('auth-alert').textContent = message;
  el('auth-alert').hidden = !message;
}

// 상단 상태 표시: 세대 웹 서버 ↔ 보관소 서버 연결
function setServiceState(online) {
  el('live-pill').classList.toggle('offline', !online);
  el('live-text').textContent = online ? '서비스 정상' : '보관소 연결 끊김';
}

function toast(message) {
  el('toast').textContent = message;
  el('toast').hidden = false;
  window.setTimeout(() => { el('toast').hidden = true; }, 2600);
}

function clearPickupCodes() {
  state.pickupCodeEpoch = (state.pickupCodeEpoch || 0) + 1;
  state.pickupCodeTimers.forEach(timer => window.clearTimeout(timer));
  state.pickupCodeTimers.clear();
  state.pickupCodes.clear();
  state.pickupCodeLoading.clear();
}

function parcelStage(parcel) {
  if (parcel.status === 'STORED' && parcel.deliveryMethod === 'ROBOT') return '택배가 보관되었습니다. 로봇 운송을 준비하고 있습니다.';
  if (parcel.status === 'STORED' && parcel.deliveryMethod === 'PICKUP') return '택배가 보관되었습니다. 출력 키오스크에서 로그인해 수령하세요.';
  if (parcel.status === 'STORED') return '수령 방법을 선택해 주세요.';
  if (parcel.status === 'IN_TRANSIT') return '로봇이 배송 중입니다.';
  if (parcel.status === 'DELIVERED') return '로봇 배송이 완료되었습니다.';
  if (parcel.status === 'PICKED_UP') return '직접 수령이 완료되었습니다.';
  return '관리자 확인이 필요한 택배입니다.';
}

function renderSummary() {
  const total = state.parcels.length;
  const stored = state.parcels.filter(parcel => parcel.status === 'STORED').length;
  const pickup = state.parcels.filter(parcel => parcel.status === 'STORED' && parcel.deliveryMethod === 'PICKUP').length;
  const moving = state.parcels.filter(parcel => parcel.status === 'IN_TRANSIT' || (parcel.status === 'STORED' && parcel.deliveryMethod === 'ROBOT')).length;
  el('summary-grid').innerHTML = [
    ['전체 택배', total, 'highlight'], ['보관함 도착', stored, ''], ['직접 수령 대기', pickup, ''], ['로봇 배송 대기·중', moving, '']
  ].map(([label, value, className]) => `<div class="summary-card ${className}"><span>${label}</span><strong>${value}</strong></div>`).join('');
  el('parcel-nav-count').textContent = total;
  el('parcel-quick-text').textContent = total ? `등록된 택배 ${total}개를 확인하세요.` : '새 택배가 등록되면 알려드려요.';
  const availableRobots = state.robots.filter(robot => robot.available).length;
  el('robot-quick-text').textContent = availableRobots ? `현재 배송 가능한 로봇 ${availableRobots}대` : '현재 이용 가능한 로봇이 없습니다.';
}

function setDashboardView(view, { updateHash = true, scroll = true } = {}) {
  const next = ['overview', 'parcels', 'robots'].includes(view) ? view : 'overview';
  state.view = next;
  el('overview-view').hidden = next !== 'overview';
  el('parcels-view').hidden = next !== 'parcels';
  el('robots-view').hidden = next !== 'robots';
  document.querySelectorAll('[data-dashboard-view]').forEach(button => {
    button.classList.toggle('active', button.dataset.dashboardView === next);
    button.setAttribute('aria-current', button.dataset.dashboardView === next ? 'page' : 'false');
  });
  if (updateHash) history.replaceState(null, '', next === 'overview' ? location.pathname : `#${next}`);
  if (scroll) window.scrollTo({ top: 0, behavior: 'smooth' });
}

function renderRobots() {
  el('robot-grid').innerHTML = state.robots.length ? state.robots.map(robot => `<article class="robot-card ${robot.connected ? '' : 'offline'}">
    <div><span class="robot-state"><i></i>${robot.connected ? 'MQTT 통신 정상' : 'MQTT 통신 끊김'}</span><h3>${safe(robot.id)}</h3>
    <p>${safe(robot.status)} · ${robot.available ? '배송 가능' : '현재 사용 불가'}</p><p>현재 위치: <strong>${safe(robot.location)}</strong></p></div>
    <div class="robot-battery">${safe(robot.battery)}%</div><p class="mqtt-topic">${safe(robot.mqttTopic)}</p></article>`).join('')
    : '<div class="empty-card">등록된 배송 로봇이 없습니다.</div>';
}

function renderDeliveryPreference() {
  const method = state.defaultDelivery === 'PICKUP' ? 'PICKUP' : 'ROBOT';
  el('default-delivery-label').textContent = method === 'ROBOT' ? '로봇 배송' : '직접 수령';
  el('preference-toggle').setAttribute('aria-expanded', String(state.preferenceOpen));
  el('delivery-preference-panel').hidden = !state.preferenceOpen;
  document.querySelectorAll('[data-default-delivery]').forEach(button => {
    const active = button.dataset.defaultDelivery === method;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
    button.disabled = state.preferenceLoading;
  });
  el('preference-server-text').textContent = state.preferenceLoading ? '서버에 저장 중…' : '서버와 동기화됨';
}

async function saveDeliveryPreference(method) {
  if (state.preferenceLoading || method === state.defaultDelivery) return;
  state.preferenceLoading = true;
  renderDeliveryPreference();
  try {
    const result = await api('/api/user/preferences', {
      method: 'PATCH', body: JSON.stringify({ defaultDelivery: method })
    });
    state.defaultDelivery = result.preference.defaultDelivery;
    state.user.defaultDelivery = result.preference.defaultDelivery;
    const label = method === 'ROBOT' ? '로봇 배송' : '직접 수령';
    clearPickupCodes();
    state.selectedParcelIds.clear();
    await loadDashboard({ quiet: true });
    toast(result.preference.changed
      ? `보관 중인 택배 ${result.preference.changed}개를 포함해 ${label}(으)로 바꿨습니다.`
      : `기본 수령 방법을 ${label}(으)로 바꿨습니다.`);
  } catch (error) {
    toast(error.message);
  } finally {
    state.preferenceLoading = false;
    renderDeliveryPreference();
  }
}

function deliverySummary(parcel) {
  if (parcel.status === 'IN_TRANSIT') return '<div class="choice-result delivery-in-progress"><strong>로봇 배송 중</strong><span>배송이 시작되어 수령 방법을 변경할 수 없습니다.</span></div>';
  if (parcel.status === 'DELIVERED' || parcel.status === 'PICKED_UP') return `<div class="choice-result completed-result"><span>${parcel.status === 'DELIVERED' ? '배송이 완료된 택배입니다.' : '직접 수령한 택배입니다.'}</span><button class="completed-remove-button" type="button" data-remove-completed="${safe(parcel.id)}" aria-label="${safe(parcel.trackingNumber || parcel.id)} 목록에서 삭제">삭제</button></div>`;
  if (!parcel.canChooseDelivery) return `<div class="choice-result">${safe(parcelStage(parcel))}</div>`;
  if (parcel.deliveryMethod === 'PICKUP') {
    const code = state.pickupCodes.get(parcel.id);
    return `<div class="choice-result pickup-result"><div class="pickup-copy"><strong>택배가 보관되었습니다.</strong><span>출력 키오스크에서 아이디로 로그인하면 바로 꺼낼 수 있습니다.</span><small class="pickup-location-text">인증코드는 관리실 확인 등에 쓰는 보조 수단입니다.</small></div><div class="pickup-code-actions"><button class="pickup-reveal-button" type="button" data-${code ? 'hide' : 'reveal'}-code="${safe(parcel.id)}" ${state.pickupCodeLoading.has(parcel.id) ? 'disabled' : ''}>${code ? '인증코드 숨기기' : state.pickupCodeLoading.has(parcel.id) ? '불러오는 중…' : '인증코드 보기'}</button>${code ? `<div class="pickup-code-block"><small>직접 수령 인증코드</small><strong class="pickup-code">${safe(code)}</strong></div>` : ''}</div></div>`;
  }
  if (parcel.deliveryMethod === 'ROBOT') return '<div class="choice-result choice-booked robot-booked"><strong>택배가 보관되었습니다.</strong><span>로봇 운송을 준비하고 있습니다.</span></div>';
  return '<div class="choice-result choice-pending"><strong>택배가 보관되었습니다.</strong><span>직접 수령 또는 로봇 배송을 선택해 주세요.</span></div>';
}

function renderSelectionBar() {
  const count = state.selectedParcelIds.size;
  const selectedParcels = state.parcels.filter(parcel => state.selectedParcelIds.has(parcel.id));
  const allChangeable = count > 0 && selectedParcels.every(parcel => parcel.canChooseDelivery);
  // 세대 기본값이 직접 수령이면 물품별 로봇 배송은 선택할 수 없다 (명세 2-1)
  const robotLocked = state.defaultDelivery === 'PICKUP';
  el('parcel-selection-bar').hidden = count === 0;
  el('selected-count').textContent = `${count}개 선택`;
  el('batch-pickup-button').disabled = !allChangeable;
  el('batch-robot-button').disabled = !allChangeable || robotLocked;
  el('selection-help').textContent = !allChangeable
    ? '배송이 시작되었거나 완료된 택배는 수령 방법을 변경할 수 없습니다.'
    : robotLocked
      ? '기본 설정이 직접 수령이라 로봇 배송은 선택할 수 없습니다. 기본 설정을 먼저 바꿔 주세요.'
      : '선택한 택배의 수령 방법을 지정하세요.';
}

function renderParcels() {
  const selectableIds = new Set(state.parcels.filter(parcel => parcel.canChooseDelivery).map(parcel => parcel.id));
  state.selectedParcelIds = new Set([...state.selectedParcelIds].filter(id => selectableIds.has(id)));
  const selectAll = el('select-all-parcels');
  el('parcel-selection-controls').hidden = state.parcels.length === 0;
  selectAll.disabled = selectableIds.size === 0;
  selectAll.checked = selectableIds.size > 0 && [...selectableIds].every(id => state.selectedParcelIds.has(id));
  selectAll.indeterminate = state.selectedParcelIds.size > 0 && !selectAll.checked;
  if (!state.parcels.length) {
    el('parcel-grid').innerHTML = '<div class="empty-card parcel-empty"><span class="empty-icon" aria-hidden="true">□</span><strong>등록된 택배가 없습니다</strong><p>택배기사가 보관소에 택배를 넣으면 이 화면에 표시됩니다.</p></div>';
    renderSelectionBar();
    return;
  }
  el('parcel-grid').innerHTML = state.parcels.map(parcel => `<article class="parcel-row ${state.selectedParcelIds.has(parcel.id) ? 'selected' : ''}">
    ${parcel.canChooseDelivery
      ? `<label class="parcel-checkbox"><input type="checkbox" data-select-parcel="${safe(parcel.id)}" ${state.selectedParcelIds.has(parcel.id) ? 'checked' : ''}><span aria-hidden="true"></span><em>${parcel.deliveryMethod ? '변경' : '선택'}</em></label>`
      : `<div class="parcel-state-rail ${parcel.status === 'IN_TRANSIT' ? 'in-transit' : ''}"><span aria-hidden="true">${parcel.status === 'IN_TRANSIT' ? '→' : '✓'}</span><em>${parcel.status === 'IN_TRANSIT' ? '배송 중' : '완료'}</em></div>`}
    <div class="parcel-row-content"><div class="parcel-top"><div><span class="parcel-id">${safe(parcel.id)}</span><h3>${safe(parcel.trackingNumber || '송장번호 미등록')}</h3></div><span class="status-pill status-${safe(parcel.status === 'STORED' && parcel.deliveryMethod === 'ROBOT' ? 'ROBOT_PREPARING' : parcel.status)}">${safe(parcel.statusLabel)}</span></div>
    <div class="parcel-inline-meta"><span>크기 <b>${safe(parcel.size)}</b></span><span>수령 방법 <b>${parcel.deliveryMethod === 'PICKUP' ? '직접 수령' : '로봇 배송'}</b></span><span>보관일 <b>${new Date(parcel.createdAt).toLocaleDateString('ko-KR')}</b></span></div>
    ${deliverySummary(parcel)}</div></article>`).join('');
  renderSelectionBar();
}

async function applyDeliveryToSelection(method) {
  const ids = [...state.selectedParcelIds];
  if (!ids.length) return;
  const hasLockedParcel = state.parcels.some(parcel => state.selectedParcelIds.has(parcel.id) && !parcel.canChooseDelivery);
  if (hasLockedParcel) {
    toast('배송이 시작되었거나 완료된 택배는 변경할 수 없습니다.');
    return;
  }
  clearPickupCodes();
  const button = el(method === 'ROBOT' ? 'batch-robot-button' : 'batch-pickup-button');
  button.disabled = true;
  button.textContent = '처리 중…';
  const results = await Promise.allSettled(ids.map(parcelId => api(`/api/user/parcels/${encodeURIComponent(parcelId)}/delivery`, {
    method: 'PATCH', body: JSON.stringify({ method })
  })));
  const failed = results.filter(result => result.status === 'rejected');
  state.selectedParcelIds.clear();
  await loadDashboard({ quiet: true });
  const methodLabel = method === 'ROBOT' ? '로봇 배송' : '직접 수령';
  button.textContent = methodLabel;
  toast(failed.length ? `${ids.length - failed.length}개 처리, ${failed.length}개 실패했습니다.` : `${ids.length}개 택배를 ${methodLabel}(으)로 선택했습니다.`);
}

function renderDashboard() {
  el('user-name').textContent = state.user.name;
  el('user-address').textContent = `${state.user.building}동 ${state.user.unit}호에 등록된 택배입니다.`;
  renderSummary();
  renderRobots();
  renderDeliveryPreference();
  renderParcels();
  setDashboardView(state.view, { updateHash: false, scroll: false });
}

function showDashboard() {
  el('auth-view').hidden = true;
  el('dashboard-view').hidden = false;
  el('header-actions').hidden = false;
}

function showAuth() {
  clearPickupCodes();
  state.user = null;
  state.parcels = [];
  el('parcel-grid').innerHTML = '';
  state.defaultDelivery = 'ROBOT';
  state.preferenceOpen = false;
  el('auth-view').hidden = false;
  el('dashboard-view').hidden = true;
  el('header-actions').hidden = true;
}

async function loadDashboard({ quiet = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  try {
    const [parcelData, robotData, preferenceData] = await Promise.all([
      api('/api/user/parcels'), api('/api/user/robots'), api('/api/user/preferences')
    ]);
    state.parcels = parcelData.parcels;
    for (const id of state.pickupCodes.keys()) {
      if (!state.parcels.some(parcel => parcel.id === id && parcel.status === 'STORED' && parcel.deliveryMethod === 'PICKUP')) {
        window.clearTimeout(state.pickupCodeTimers.get(id));
        state.pickupCodeTimers.delete(id);
        state.pickupCodes.delete(id);
      }
    }
    state.robots = robotData.robots;
    state.defaultDelivery = preferenceData.preference.defaultDelivery;
    setServiceState(true);
    renderDashboard();
    if (!quiet) toast('최신 상태로 업데이트했습니다.');
  } catch (error) {
    if (error.status === 401) showAuth();
    else if (error.status === 503) { setServiceState(false); if (!quiet) toast(error.message); }
    else toast(error.message);
  } finally {
    state.loading = false;
  }
}

async function handleAuth(form, path) {
  const data = Object.fromEntries(new FormData(form));
  authError('');
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const result = await api(path, { method: 'POST', body: JSON.stringify(data) });
    state.user = result.user;
    state.defaultDelivery = result.user.defaultDelivery || 'ROBOT';
    showDashboard();
    await loadDashboard({ quiet: true });
  } catch (error) {
    authError(error.message);
  } finally {
    button.disabled = false;
  }
}

document.addEventListener('click', async event => {
  const dashboardNav = event.target.closest('[data-dashboard-view]');
  const quickLink = event.target.closest('[data-open-view]');
  if (dashboardNav || quickLink) {
    setDashboardView(dashboardNav ? dashboardNav.dataset.dashboardView : quickLink.dataset.openView);
    return;
  }
  if (event.target.closest('#refresh-button')) { await loadDashboard(); return; }
  const revealCode = event.target.closest('[data-reveal-code]');
  if (revealCode) {
    const id = revealCode.dataset.revealCode;
    if (state.pickupCodeLoading.has(id)) return;
    const revealEpoch = state.pickupCodeEpoch || 0;
    const userId = state.user?.id;
    state.pickupCodeLoading.add(id);
    renderParcels();
    try {
      const result = await api(`/api/user/parcels/${encodeURIComponent(id)}/pickup-code`, { cache: 'no-store' });
      if (revealEpoch !== (state.pickupCodeEpoch || 0) || userId !== state.user?.id) return;
      state.pickupCodes.set(id, result.code);
      window.clearTimeout(state.pickupCodeTimers.get(id));
      state.pickupCodeTimers.set(id, window.setTimeout(() => {
        state.pickupCodes.delete(id);
        state.pickupCodeTimers.delete(id);
        renderParcels();
      }, 60000));
    } catch (error) { toast(error.message); }
    finally {
      state.pickupCodeLoading.delete(id);
      renderParcels();
    }
    return;
  }
  const hideCode = event.target.closest('[data-hide-code]');
  if (hideCode) {
    const id = hideCode.dataset.hideCode;
    window.clearTimeout(state.pickupCodeTimers.get(id));
    state.pickupCodeTimers.delete(id);
    state.pickupCodes.delete(id);
    renderParcels();
    return;
  }
  const removeCompleted = event.target.closest('[data-remove-completed]');
  if (removeCompleted) {
    const parcelId = removeCompleted.dataset.removeCompleted;
    if (!window.confirm('완료된 택배를 내 목록에서 삭제할까요?')) return;
    removeCompleted.disabled = true;
    try {
      await api(`/api/user/parcels/${encodeURIComponent(parcelId)}`, { method: 'DELETE' });
      await loadDashboard({ quiet: true });
      toast('완료된 택배를 내 목록에서 삭제했습니다.');
    } catch (error) {
      removeCompleted.disabled = false;
      toast(error.message);
    }
    return;
  }
  if (event.target.closest('#preference-toggle')) {
    state.preferenceOpen = !state.preferenceOpen;
    renderDeliveryPreference();
    return;
  }
  const preferenceOption = event.target.closest('[data-default-delivery]');
  if (preferenceOption) {
    await saveDeliveryPreference(preferenceOption.dataset.defaultDelivery);
    return;
  }
  if (event.target.closest('#batch-robot-button')) { await applyDeliveryToSelection('ROBOT'); return; }
  if (event.target.closest('#batch-pickup-button')) { await applyDeliveryToSelection('PICKUP'); return; }
  if (event.target.closest('#clear-selection-button')) {
    state.selectedParcelIds.clear();
    renderParcels();
    return;
  }
  if (event.target.closest('#logout-button')) {
    await api('/api/auth/logout', { method: 'POST' }).catch(() => {});
    showAuth();
    authError('');
    state.view = 'overview';
    history.replaceState(null, '', location.pathname);
    return;
  }
});

document.addEventListener('change', event => {
  const parcelCheckbox = event.target.closest('[data-select-parcel]');
  if (parcelCheckbox) {
    if (parcelCheckbox.checked) state.selectedParcelIds.add(parcelCheckbox.dataset.selectParcel);
    else state.selectedParcelIds.delete(parcelCheckbox.dataset.selectParcel);
    renderParcels();
    return;
  }
  if (event.target.closest('#select-all-parcels')) {
    if (event.target.checked) {
      state.parcels.filter(parcel => parcel.canChooseDelivery).forEach(parcel => state.selectedParcelIds.add(parcel.id));
    } else state.selectedParcelIds.clear();
    renderParcels();
  }
});

el('login-form').addEventListener('submit', event => { event.preventDefault(); handleAuth(event.currentTarget, '/api/auth/login'); });

async function boot() {
  try {
    const result = await api('/api/me');
    state.user = result.user;
    showDashboard();
    await loadDashboard({ quiet: true });
  } catch {
    showAuth();
    authError('');
  }
}

window.setInterval(() => { if (state.user) loadDashboard({ quiet: true }); }, 10000);
window.addEventListener('hashchange', () => setDashboardView(location.hash.slice(1) || 'overview', { updateHash: false }));
boot();
