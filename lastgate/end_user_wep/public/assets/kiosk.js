const routeMode = location.pathname === '/intake' ? 'register' : location.pathname === '/pickup' ? 'pickup' : null;
const state = {
  mode: routeMode || 'home',
  registrationStarted: false,
  step: 1,
  buildings: ['101', '102', '103', '104', '105', '106'],
  building: '101', unit: '', size: 'M', weightKg: '', trackingNumber: '', lockerId: '',
  requestId: crypto.randomUUID(), submitting: false, result: null,
  pickup: { step: 1, code: '', submitting: false, result: null, completed: null }
};
const el = id => document.getElementById(id);
const safe = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
const ICONS = {
  arrow: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14m-6-6 6 6-6 6"/></svg>',
  check: '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round"><path d="m5 12 4.5 4.5L19 7"/></svg>',
  package: '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><path d="m12 2 9 5-9 5-9-5 9-5Zm-9 5v10l9 5 9-5V7M12 12v10M7.5 4.5l9 5"/></svg>',
  locker: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="2" width="18" height="20" rx="2"/><path d="M3 12h18M12 2v20M8.5 7h.01M15.5 17h.01"/></svg>'
};

function setError(message = '') {
  const alert = el('kiosk-alert');
  alert.textContent = message;
  alert.hidden = !message;
}
function displayClock() {
  el('local-clock').textContent = new Intl.DateTimeFormat('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date());
}
function renderSideSteps() {
  const isPickup = state.mode === 'pickup';
  const currentStep = isPickup ? state.pickup.step : state.step;
  const steps = isPickup ? [
    ['01', '인증코드 확인', '6자리 코드 입력'],
    ['02', '보관 위치 확인', '보관함 위치 안내'],
    ['03', '수령 완료', '택배 수령 처리']
  ] : [
    ['01', '배송 주소', '도착할 동·호수 입력'],
    ['02', '택배 정보', '크기와 송장번호 등록'],
    ['03', '접수 확인', '정보 확인 및 접수']
  ];
  el('side-steps').innerHTML = steps.map((s, i) => {
    const number = i + 1;
    const complete = (!isPickup && state.step === 4) || number < currentStep;
    return `<div class="side-step ${number === currentStep ? 'side-step-active' : ''} ${complete ? 'side-step-done' : ''}">
      <span class="side-step-number">${complete ? ICONS.check : s[0]}</span>
      <span><strong>${s[1]}</strong><small>${s[2]}</small></span>
    </div>`;
  }).join('');
}
function renderHome() {
  return `<div class="mode-home"><div class="success-eyebrow">WELCOME TO LASTGATE</div>
    <h1>원하시는 업무를 선택해 주세요<span class="mint-question">.</span></h1>
    <p class="page-lead">화면의 큰 버튼을 눌러 택배 수령 또는 등록을 시작할 수 있습니다.</p>
    <div class="mode-grid">
      <button class="mode-card mode-card-primary" id="start-pickup" type="button"><span class="mode-icon">${ICONS.locker}</span><strong>택배 직접 수령</strong><small>인증코드로 보관 위치를 확인하고<br>택배를 직접 수령합니다.</small></button>
      <button class="mode-card" id="start-register" type="button"><span class="mode-icon">${ICONS.package}</span><strong>택배 등록</strong><small>배송 주소와 택배 정보를 입력해<br>새 택배를 접수합니다.</small></button>
    </div></div>`;
}
function renderRegisterStart() {
  return `<div class="intake-start-stage">
    <div class="intake-start-icon" aria-hidden="true">${ICONS.package}</div>
    <div class="success-eyebrow">PARCEL INTAKE</div>
    <h1>택배 등록을 시작해 주세요<span class="mint-question">.</span></h1>
    <p class="page-lead">배송 주소와 택배 정보를 차례대로 입력하면 안전하게 접수됩니다.</p>
    <div class="intake-start-guide" aria-label="택배 등록 순서">
      <span><b>1</b>배송 주소 입력</span><span><b>2</b>택배 정보 입력</span><span><b>3</b>접수 내용 확인</span>
    </div>
    <button class="btn btn-primary intake-start-button" id="begin-register" type="button">시작하기 ${ICONS.arrow}</button>
  </div>`;
}
function renderStep1() {
  return `<h1>어디로 보내시나요<span class="mint-question">?</span></h1>
    <p class="page-lead">택배가 배송될 동과 호수를 선택해 주세요.</p>
    <div class="form-section"><div class="form-label-row"><label>아파트 동 선택 <span class="required">*</span></label><span class="field-sub">총 6개 동</span></div>
      <div class="building-grid">${state.buildings.map(b => `<button class="building-card ${state.building === b ? 'selected' : ''}" type="button" data-building="${safe(b)}" aria-pressed="${state.building === b}"><span class="building-icon">▥</span><span>${safe(b)}동</span><span class="card-corner">${state.building === b ? ICONS.check : ''}</span></button>`).join('')}</div>
    </div>
    <div class="form-section unit-section"><div class="form-label-row"><label for="unit-input">호수 입력 <span class="required">*</span></label><span class="field-sub">숫자만 입력</span></div>
      <div class="unit-field"><input id="unit-input" inputmode="numeric" type="text" maxlength="4" placeholder="예: 1203" autocomplete="off" value="${safe(state.unit)}"><span class="unit-suffix">호</span></div>
      <p class="helper-text">동·호수가 정확하지 않으면 택배가 잘못 분류될 수 있습니다.</p>
    </div>
    <div class="inline-info"><span class="info-icon">i</span> 수취인 이름·전화번호는 수집하지 않는 시연용 키오스크입니다.</div>`;
}
function renderStep2() {
  const cards = [
    { size: 'S', label: '소형', desc: '문서, 작은 소포', icon: '▣' },
    { size: 'M', label: '중형', desc: '일반 택배 상자', icon: '▤' },
    { size: 'L', label: '대형', desc: '부피가 큰 상자', icon: '▥' }
  ];
  return `<h1>택배 정보를 알려주세요<span class="mint-question">.</span></h1>
    <p class="page-lead">보관 구역 분류에 사용할 크기와 송장번호를 입력합니다.</p>
    <div class="form-section"><div class="form-label-row"><label>택배 크기 <span class="required">*</span></label><span class="field-sub">해당하는 크기를 선택</span></div><div class="size-grid">
    ${cards.map(c => `<button class="size-card ${state.size === c.size ? 'selected' : ''}" type="button" data-size="${c.size}" aria-pressed="${state.size === c.size}"><span class="size-art">${c.icon}</span><span class="size-code">${c.size}</span><strong>${c.label}</strong><small>${c.desc}</small><span class="size-check">${state.size === c.size ? ICONS.check : ''}</span></button>`).join('')}</div></div>
    <div class="form-section"><div class="form-label-row"><label for="tracking-input">송장번호 <span class="optional">선택</span></label><span class="field-sub">바코드 스캐너 입력 가능</span></div><div class="unit-field"><input type="text" id="tracking-input" maxlength="40" placeholder="영문·숫자 6~40자리" autocomplete="off" value="${safe(state.trackingNumber)}"></div><p class="helper-text">송장번호가 없어도 접수할 수 있으며 접수번호는 서버에서 자동 생성됩니다.</p></div>
    <div class="form-section"><div class="form-label-row"><label for="locker-input">실제 보관 위치 <span class="required">*</span></label><span class="field-sub">직접 수령 안내에 표시</span></div><div class="unit-field"><input id="locker-input" type="text" maxlength="24" placeholder="예: A-01" autocomplete="off" value="${safe(state.lockerId)}"></div><p class="helper-text">택배를 넣은 보관함 또는 선반의 위치를 정확히 입력해 주세요.</p></div>
    <div class="form-section weight-section"><div class="form-label-row"><label for="weight-input">무게 <span class="optional">선택</span></label><span class="field-sub">최대 30kg · 시연용 제한</span></div><div class="unit-field"><input id="weight-input" type="number" inputmode="decimal" min="0.1" max="30" step="0.1" placeholder="예: 2.5" value="${safe(state.weightKg)}"><span class="unit-suffix">kg</span></div></div>`;
}
function renderStep3() {
  return `<h1>마지막으로 확인해 주세요<span class="mint-question">.</span></h1>
    <p class="page-lead">접수 버튼을 누르면 서버에 택배 정보가 저장됩니다.</p>
    <div class="review-card"><div class="review-header"><span class="review-header-icon">${ICONS.package}</span><div><strong>택배 접수 정보</strong><small>PARCEL INFORMATION</small></div><span class="review-check">입력 완료</span></div>
    <div class="review-row"><span>배송 목적지</span><strong>${safe(state.building)}동 ${safe(state.unit)}호</strong></div>
    <div class="review-row"><span>택배 크기</span><strong>${safe(state.size)} · ${safe({S:'소형',M:'중형',L:'대형'}[state.size])}</strong></div>
    <div class="review-row"><span>송장번호</span><strong>${safe(state.trackingNumber || '미입력')}</strong></div>
    <div class="review-row"><span>무게</span><strong>${state.weightKg ? `${safe(state.weightKg)}kg` : '미입력'}</strong></div>
    <div class="review-row"><span>보관 위치</span><strong class="mint-text">${safe(state.lockerId)}</strong></div>
    </div>
    <div class="inline-info"><span class="info-icon">i</span> 택배가 보관소에 놓인 뒤 접수해 주세요. 완료 시 보관 위치가 사용자 웹에 표시됩니다.</div>`;
}
function renderSuccess() {
  const parcel = state.result || {};
  const status = parcel.status === 'STORED' ? '보관 완료' : '거점 접수 완료';
  return `<div class="success-stage"><div class="success-icon">${ICONS.check}</div><div class="success-eyebrow">PARCEL REGISTERED</div><h1>접수가 완료되었습니다<span class="mint-question">!</span></h1><p class="page-lead">택배 정보가 서버에 안전하게 등록되었습니다.</p>
    <div class="receipt-card"><div class="receipt-head"><span>접수 번호</span><span class="receipt-status"><span class="tiny-pulse"></span>${safe(status)}</span></div><div class="receipt-code" id="receipt-code">${safe(parcel.id)}</div>
    <div class="receipt-row"><span>배송 목적지</span><strong>${safe(parcel.building)}동 ${safe(parcel.unit)}호</strong></div>
    <div class="receipt-row"><span>보관 위치</span><strong>${safe(parcel.lockerId || parcel.sortZone)}</strong></div>
    <div class="receipt-row"><span>접수 시각</span><strong>${new Date(parcel.createdAt).toLocaleString('ko-KR')}</strong></div>
    </div>
    <p class="helper-text success-hint">직접 수령 인증코드는 로그인한 사용자 웹의 내 택배 화면에서 확인할 수 있습니다.</p></div>`;
}
function renderPickupCode() {
  return `<h1>인증코드를 입력해 주세요<span class="mint-question">.</span></h1>
    <p class="page-lead">안내받은 6자리 인증코드로 수령할 택배를 확인합니다.</p>
    <div class="form-section"><div class="form-label-row"><label for="pickup-code-input">인증코드 <span class="required">*</span></label><span class="field-sub">숫자 6자리</span></div>
      <div class="unit-field pickup-code-field"><input id="pickup-code-input" inputmode="numeric" type="text" maxlength="6" pattern="[0-9]*" placeholder="000000" autocomplete="one-time-code" value="${safe(state.pickup.code)}"></div>
      <p class="helper-text pickup-code-hint">택배가 보관 완료 상태일 때만 직접 수령할 수 있습니다.</p>
    </div>
    <div class="inline-info"><span class="info-icon">i</span> 인증코드는 로그인한 사용자 웹의 내 택배 화면에서 확인하세요.</div>`;
}
function renderPickupLocation() {
  const pickup = state.pickup.result || {};
  return `<h1>보관 위치를 확인해 주세요<span class="mint-question">.</span></h1>
    <p class="page-lead">아래 위치에서 택배를 꺼낸 뒤 수령 완료 버튼을 눌러 주세요.</p>
    <div class="pickup-location"><span class="pickup-location-label">택배 보관 위치</span><strong class="pickup-location-value">${safe(pickup.location)}</strong><span class="pickup-address">${safe(pickup.building)}동 ${safe(pickup.unit)}호 · 접수번호 ${safe(pickup.id)}</span></div>
    <div class="inline-info"><span class="info-icon">i</span> 택배를 실제로 꺼낸 후에만 수령 완료를 눌러 주세요. 완료 후에는 되돌릴 수 없습니다.</div>`;
}
function renderPickupSuccess() {
  const parcel = state.pickup.completed || {};
  return `<div class="success-stage"><div class="success-icon">${ICONS.check}</div><div class="success-eyebrow">PICKUP COMPLETED</div>
    <h1>수령이 완료되었습니다<span class="mint-question">!</span></h1><p class="page-lead">택배가 수령 완료 상태로 안전하게 처리되었습니다.</p>
    <div class="receipt-card"><div class="receipt-head"><span>접수 번호</span><span class="receipt-status"><span class="tiny-pulse"></span>수령 완료</span></div>
    <div class="receipt-code">${safe(parcel.id)}</div><div class="receipt-row"><span>보관 위치</span><strong class="pickup-success-location">${safe(state.pickup.result?.location)}</strong></div>
    <div class="receipt-row"><span>수령 시각</span><strong>${parcel.updatedAt ? new Date(parcel.updatedAt).toLocaleString('ko-KR') : ''}</strong></div></div></div>`;
}
function renderFooter() {
  let html;
  if (state.mode === 'home' || (state.mode === 'register' && !state.registrationStarted)) html = '';
  else if (state.mode === 'pickup') {
    if (state.pickup.step === 1) html = `<button class="btn btn-secondary" id="go-home" type="button">처음 화면</button><button class="btn btn-primary" id="verify-pickup" ${state.pickup.submitting ? 'disabled' : ''} type="button">${state.pickup.submitting ? '인증 중…' : '인증코드 확인'} ${state.pickup.submitting ? '' : ICONS.arrow}</button>`;
    else if (state.pickup.step === 2) html = `<button class="btn btn-secondary" id="go-home" type="button">수령 취소</button><button class="btn btn-primary" id="complete-pickup" ${state.pickup.submitting ? 'disabled' : ''} type="button">${state.pickup.submitting ? '처리 중…' : '수령 완료'} ${state.pickup.submitting ? '' : ICONS.arrow}</button>`;
    else html = `<button class="btn btn-primary" id="go-home" type="button">처음 화면으로 ${ICONS.arrow}</button>`;
  } else if (state.step === 4) html = `<button class="btn btn-secondary" id="go-home" type="button">처음 화면</button><button class="btn btn-primary" id="register-next" type="button">다음 택배 등록 ${ICONS.arrow}</button>`;
  else if (state.step === 1) html = `<button class="btn btn-secondary" id="go-home" type="button">처음 화면</button><button class="btn btn-primary" id="go-next" type="button">다음 단계 ${ICONS.arrow}</button>`;
  else if (state.step === 3) html = `<button class="btn btn-secondary" id="go-prev" type="button">← 이전</button><button class="btn btn-primary" id="submit-parcel" ${state.submitting ? 'disabled' : ''} type="button">${state.submitting ? '서버에 접수 중…' : '택배 접수 완료'} ${state.submitting ? '' : ICONS.arrow}</button>`;
  else html = `<button class="btn btn-secondary" id="go-prev" type="button">← 이전</button><button class="btn btn-primary" id="go-next" type="button">다음 단계 ${ICONS.arrow}</button>`;
  el('footer-actions').innerHTML = html;
}
function render() {
  const isHome = state.mode === 'home';
  const isRegisterStart = state.mode === 'register' && !state.registrationStarted;
  el('stepper-bar').hidden = isHome || isRegisterStart;
  el('kiosk-eyebrow').textContent = isHome ? 'LASTGATE KIOSK' : state.mode === 'pickup' ? 'PARCEL PICKUP' : 'PARCEL INTAKE';
  if (!isHome && !isRegisterStart) {
    const current = state.mode === 'pickup' ? state.pickup.step : state.step;
    const complete = state.mode === 'pickup' ? current === 3 : current === 4;
    el('progress-text').textContent = complete ? 'COMPLETED' : `STEP 0${current} / 03`;
    renderSideSteps();
  }
  if (isHome) el('step-content').innerHTML = renderHome();
  else if (isRegisterStart) el('step-content').innerHTML = renderRegisterStart();
  else if (state.mode === 'pickup') el('step-content').innerHTML = [null, renderPickupCode, renderPickupLocation, renderPickupSuccess][state.pickup.step]();
  else el('step-content').innerHTML = [null, renderStep1, renderStep2, renderStep3, renderSuccess][state.step]();
  renderFooter();
  setError();
}
function captureInputs() {
  if (el('unit-input')) state.unit = el('unit-input').value.trim();
  if (el('tracking-input')) state.trackingNumber = el('tracking-input').value.trim().toUpperCase();
  if (el('locker-input')) state.lockerId = el('locker-input').value.trim().toUpperCase();
  if (el('weight-input')) state.weightKg = el('weight-input').value.trim();
  if (el('pickup-code-input')) state.pickup.code = el('pickup-code-input').value.replace(/\D/g, '').slice(0, 6);
}
function validateStep() {
  if (state.step === 1 && !/^[1-9]\d{0,3}$/.test(state.unit)) return '호수를 1~4자리 숫자로 입력해 주세요.';
  if (state.step === 2 && state.trackingNumber && !/^[A-Z0-9-]{6,40}$/.test(state.trackingNumber)) return '송장번호는 영문·숫자·하이픈 6~40자로 입력해 주세요.';
  if (state.step === 2 && !/^[A-Z0-9-]{1,24}$/.test(state.lockerId)) return '실제 보관 위치를 영문·숫자·하이픈 1~24자로 입력해 주세요.';
  if (state.step === 2 && state.weightKg && (!Number.isFinite(Number(state.weightKg)) || Number(state.weightKg) <= 0 || Number(state.weightKg) > 30)) return '무게는 0보다 크고 30kg 이하여야 합니다.';
  return '';
}
async function submit() {
  captureInputs();
  if (state.submitting) return;
  state.submitting = true;
  renderFooter();
  try {
    const resp = await fetch('/api/parcels', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        building: state.building, unit: state.unit, size: state.size,
        trackingNumber: state.trackingNumber || null,
        storedAtIntake: true, lockerId: state.lockerId,
        weightKg: state.weightKg === '' ? null : Number(state.weightKg),
        requestId: state.requestId
      })
    });
    const result = await resp.json();
    if (!resp.ok) throw new Error(result?.error?.message || '접수에 실패했습니다.');
    state.result = result.parcel;
    state.step = 4;
    render();
  } catch (error) {
    setError(`${error.message} 네트워크 오류라면 같은 버튼으로 안전하게 재시도할 수 있습니다.`);
  } finally {
    state.submitting = false;
    renderFooter();
  }
}
function resetRegistration() {
  Object.assign(state, {
    step: 1, building: '101', unit: '', size: 'M', trackingNumber: '', lockerId: '',
    weightKg: '', requestId: crypto.randomUUID(), submitting: false, result: null
  });
  render();
}
function goHome() {
  state.pickup = { step: 1, code: '', submitting: false, result: null, completed: null };
  if (routeMode === 'register') {
    state.mode = 'register';
    state.registrationStarted = false;
    resetRegistration();
    return;
  }
  if (routeMode === 'pickup') {
    state.mode = 'pickup';
    render();
    return;
  }
  state.mode = 'home';
  render();
}
async function verifyPickupCode() {
  captureInputs();
  if (!/^\d{6}$/.test(state.pickup.code)) { setError('인증코드 6자리를 입력해 주세요.'); return; }
  if (state.pickup.submitting) return;
  state.pickup.submitting = true;
  renderFooter();
  try {
    const resp = await fetch('/api/pickups/verify', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: state.pickup.code })
    });
    const result = await resp.json();
    if (!resp.ok) throw new Error(result?.error?.message || '인증코드를 확인할 수 없습니다.');
    state.pickup.result = result.pickup;
    state.pickup.step = 2;
    render();
  } catch (error) {
    setError(error.message);
  } finally {
    state.pickup.submitting = false;
    renderFooter();
  }
}
async function completePickupRequest() {
  if (state.pickup.submitting || !state.pickup.result) return;
  state.pickup.submitting = true;
  renderFooter();
  try {
    const resp = await fetch('/api/pickups/complete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: state.pickup.code, parcelId: state.pickup.result.id })
    });
    const result = await resp.json();
    if (!resp.ok) throw new Error(result?.error?.message || '수령 완료 처리에 실패했습니다.');
    state.pickup.completed = result.parcel;
    state.pickup.step = 3;
    render();
  } catch (error) {
    setError(error.message);
  } finally {
    state.pickup.submitting = false;
    renderFooter();
  }
}

document.addEventListener('click', async event => {
  if (event.target.closest('#start-pickup')) { state.mode = 'pickup'; render(); return; }
  if (event.target.closest('#start-register')) { state.mode = 'register'; state.registrationStarted = false; resetRegistration(); return; }
  if (event.target.closest('#begin-register')) { state.registrationStarted = true; resetRegistration(); return; }
  if (event.target.closest('#go-home')) { goHome(); return; }
  if (event.target.closest('#verify-pickup')) { await verifyPickupCode(); return; }
  if (event.target.closest('#complete-pickup')) { await completePickupRequest(); return; }
  if (event.target.closest('#register-next')) { resetRegistration(); return; }
  const b = event.target.closest('[data-building]');
  if (b) { captureInputs(); state.building = b.dataset.building; render(); return; }
  const s = event.target.closest('[data-size]');
  if (s) { captureInputs(); state.size = s.dataset.size; render(); return; }
  if (event.target.closest('#go-next')) {
    captureInputs();
    const error = validateStep(); if (error) { setError(error); return; }
    state.step += 1; render(); return;
  }
  if (event.target.closest('#go-prev')) { captureInputs(); state.step -= 1; render(); return; }
  if (event.target.closest('#submit-parcel')) { await submit(); return; }
  if (event.target.closest('#copy-receipt')) {
    try { await navigator.clipboard.writeText(state.result?.id || ''); event.target.textContent = '복사 완료 ✓'; }
    catch { setError('클립보드 접근이 허용되지 않았습니다. 접수번호를 직접 복사해 주세요.'); }
  }
});

displayClock();
setInterval(displayClock, 30_000);
fetch('/api/config').then(r => r.ok ? r.json() : Promise.reject()).then(config => {
  if (Array.isArray(config.buildings) && config.buildings.length) { state.buildings = config.buildings; render(); }
}).catch(() => setError('서버 연결 상태를 확인해 주세요. 접수를 위해 서버가 실행 중이어야 합니다.'));
render();
