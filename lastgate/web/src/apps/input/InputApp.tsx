// 입력 키오스크 (택배기사): 배송 주소 → 보관 구분 → 접수 확인 → 투입 안내(최소 정보)
// 건물·호실은 관리자 초기 설정(locker/config/site)을 그대로 쓴다.
import { useEffect, useRef, useState } from "react";
import {
  TOPICS, TRACKING_RE, checkUnit, keyLeadsToUnit, kioskId, limitText, unitLabel,
} from "../../shared/config";
import { useLocker, useNow, useSite, useTodayCount } from "../../shared/hooks";
import {
  IconArrowDown, IconBuilding, IconCheck, IconDoor, IconFragile, IconInfo, IconOversize, IconPackage,
} from "../../shared/icons";
import {
  Brand, Card, ConnectionBadge, Eyebrow, InfoNote, Keypad, OptionCard, PrimaryButton, SecondaryButton,
  SectionLabel, SiteGate, Stepper, Title, applyKey,
} from "../../shared/ui";

type Step = 1 | 2 | 3 | 4;
type Area = "GRID" | "OVERSIZE";
type InsertState = "waiting" | "confirming" | "done" | "cancelled";
type Receipt = { no: string; area: Area; door: number | null; message: string; deadline: number; total: number };

const STEPS = ["배송 주소", "보관 구분", "접수 확인"];
const NEXT_ITEM_DELAY_MS = 3000;
const pad2 = (n: number | null) => String(n ?? 0).padStart(2, "0");

export default function InputApp() {
  const kiosk = kioskId("INPUT-01");
  const { client, connected } = useLocker(kiosk);
  const [step, setStep] = useState<Step>(1);
  const [building, setBuilding] = useState<number | null>(null);
  const [unit, setUnit] = useState("");
  const [area, setArea] = useState<Area | null>(null);
  const [fragile, setFragile] = useState(false);
  const [tracking, setTracking] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [insert, setInsert] = useState<InsertState>("waiting");
  const [cancelReason, setCancelReason] = useState("");
  const [todayCount, applyTodayCount] = useTodayCount(client, connected, kiosk);
  const [endOpen, setEndOpen] = useState(false);
  const unitRef = useRef<HTMLInputElement>(null);
  const receiptRef = useRef<string | null>(null);
  const now = useNow();
  // 설정이 바뀌면 진행 중인 접수가 없을 때 새로 고친다
  const { site, loaded } = useSite(client, step === 1 && unit === "" && !receipt);

  const buildings = site?.buildings ?? [];
  const current = buildings.find((b) => b.code === building) ?? null;
  const single = buildings.length === 1;
  const unitCheck = checkUnit(current, site?.unit_term ?? "호", unit);
  const trackingError = tracking && !TRACKING_RE.test(tracking) ? "송장번호는 영문·숫자 6~40자리입니다." : null;
  const canNext1 = current !== null && unitCheck.state === "ok";
  const canNext2 = area !== null && !trackingError;
  const remaining = receipt ? Math.max(0, Math.ceil((receipt.deadline - now) / 1000)) : 0;
  const locked = area === "OVERSIZE" || fragile;

  receiptRef.current = receipt?.no ?? null;

  // 건물이 하나뿐이면 고르는 단계를 건너뛴다
  useEffect(() => {
    if (single && building !== buildings[0].code) setBuilding(buildings[0].code);
    if (building !== null && !buildings.some((b) => b.code === building)) setBuilding(null);
  }, [site]);

  // 서버가 투입 시간 초과로 접수를 취소하면 알림을 받는다
  useEffect(() => {
    if (!client) return;
    return client.onPush((msg) => {
      if (msg.type === "REGISTER_CANCELLED" && msg.receipt_no === receiptRef.current) {
        setInsert("cancelled");
        setCancelReason(msg.message ?? "투입이 확인되지 않아 접수가 취소되었습니다.");
      }
    });
  }, [client]);

  useEffect(() => {
    if (step === 4 && insert === "waiting" && receipt && remaining === 0) {
      setInsert("cancelled");
      setCancelReason("투입 시간이 지나 접수가 취소되었습니다.");
    }
  }, [step, insert, receipt, remaining]);

  useEffect(() => {
    if (insert !== "done") return;
    const t = window.setTimeout(nextItem, NEXT_ITEM_DELAY_MS);
    return () => window.clearTimeout(t);
  }, [insert]);

  useEffect(() => {
    if (step === 1 && current) unitRef.current?.focus();
  }, [step, current]);

  function nextItem() {
    // 같은 건물에 연속 배송하는 경우가 많으므로 건물은 유지
    setUnit("");
    setArea(null);
    setFragile(false);
    setTracking("");
    setReceipt(null);
    setInsert("waiting");
    setError(null);
    setStep(1);
  }

  function resetAll() {
    nextItem();
    if (!single) setBuilding(null);
  }

  function retry() {
    setReceipt(null);
    setInsert("waiting");
    setError(null);
    setStep(3);
  }

  function chooseBuilding(code: number) {
    if (code !== building) setUnit("");
    setBuilding(code);
  }

  async function submit() {
    if (!client || !area || building === null) return;
    setBusy(true);
    setError(null);
    try {
      const res = await client.request(TOPICS.register, {
        building, unit: Number(unit), area, fragile, tracking_no: tracking || null, kiosk_id: kiosk,
      });
      if (res.status === "OK") {
        const total = Number(res.timeout_sec) || 60;
        setReceipt({ no: res.receipt_no, area: res.storage_area, door: res.door ?? null, message: res.message,
                     deadline: Date.now() + total * 1000, total });
        setInsert("waiting");
        setStep(4);
      } else {
        setError(res.message ?? "접수할 수 없습니다.");
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function confirmInsert() {
    if (!client || !receipt) return;
    setInsert("confirming");
    setError(null);
    try {
      const res = await client.request(TOPICS.portConfirm, {
        receipt_no: receipt.no, port: receipt.area, source: "KIOSK",
      });
      if (res.status === "OK") {
        setInsert("done");
        applyTodayCount(res);
      } else if (res.status === "CANCELLED") {
        setInsert("cancelled");
        setCancelReason(res.message);
      } else {
        setInsert("waiting");
        setError(res.message ?? "투입을 확인하지 못했습니다.");
      }
    } catch (e) {
      setInsert("waiting");
      setError((e as Error).message);
    }
  }

  const unitTerm = site?.unit_term ?? "호";
  const buildingTerm = site?.building_term ?? "동";

  return (
    <div className="kiosk">
      <header className="topbar">
        <div className="topbar-left">
          <Brand />
          <ConnectionBadge connected={connected} />
        </div>
        <Stepper steps={STEPS} current={step} />
        <div className="topbar-right">
          <span className="step-counter">STEP {String(Math.min(step, 3)).padStart(2, "0")} / 03</span>
          <span className="chip">오늘 등록 <strong>{todayCount ?? "-"}</strong>건</span>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setEndOpen(true)}
                  disabled={step === 4 && insert !== "done"}>
            업무 종료
          </button>
        </div>
      </header>

      <main className="main">
        {!site && <SiteGate loaded={loaded} connected={connected} />}

        {site && step === 1 && (
          <Card>
            <Eyebrow>Parcel Intake · {site.name}</Eyebrow>
            <Title text="어디로 보내시나요" mark="?" />
            <p className="subtitle">
              {single ? `${current?.name ?? ""} ${unitTerm} 번호를 입력해 주세요.`
                      : `배송될 ${buildingTerm}을(를) 고르고 ${unitTerm} 번호를 입력해 주세요.`}
            </p>

            {!single && (
              <>
                <SectionLabel title={`${buildingTerm} 선택`} required hint={`총 ${buildings.length}개`} />
                <div className="grid-3">
                  {buildings.map((b) => (
                    <OptionCard key={b.code} selected={building === b.code} onClick={() => chooseBuilding(b.code)}>
                      <span className="option-icon"><IconBuilding /></span>
                      <span className="option-title">{b.name}</span>
                    </OptionCard>
                  ))}
                </div>
              </>
            )}

            {current && (
              <>
                <SectionLabel title={`${unitTerm} 입력`} required hint="숫자만 입력" />
                <div className="unit-guide"><IconInfo size={18} /><span><b>{current.name}</b> · {current.guide}</span></div>
                <div className="unit-grid">
                  <div>
                    <label className={`field ${unitCheck.state === "error" ? "invalid" : ""}`}>
                      <input
                        ref={unitRef} inputMode="numeric" placeholder={`예: ${current.units[0]}`} value={unit}
                        maxLength={4}
                        onChange={(e) => setUnit(e.target.value.replace(/\D/g, "").slice(0, 4))}
                        onKeyDown={(e) => {
                          // 상태 대신 입력창 값으로 판단 (입력 직후 Enter 가 와도 안전)
                          const value = e.currentTarget.value;
                          if (e.key === "Enter" && checkUnit(current, unitTerm, value).state === "ok") setStep(2);
                        }}
                      />
                      <span className="field-suffix">{unitTerm}</span>
                    </label>
                    {unitCheck.state === "error"
                      ? <p className="field-error">{unitCheck.message}</p>
                      : <p className="field-help">
                          {unitCheck.state === "ok" ? `${unitLabel(site, building, unit)} 로 접수합니다.`
                            : unitCheck.message ?? "없는 호실로만 이어지는 숫자 키는 눌리지 않습니다."}
                        </p>}
                  </div>
                  <Keypad onKey={(k) => setUnit((v) => applyKey(v, k, 4))}
                          enabled={(d) => keyLeadsToUnit(current.units, unit, d)} />
                </div>
              </>
            )}

            <InfoNote>수취인 이름·전화번호는 수집하지 않습니다.</InfoNote>
          </Card>
        )}

        {site && step === 2 && (
          <Card>
            <Eyebrow>Parcel Intake</Eyebrow>
            <Title text="어디에 보관할까요" mark="?" />
            <p className="subtitle">입력구 틀에 들어가지 않으면 규격 초과를 선택하세요.</p>

            <SectionLabel title="보관 구분" required hint={`틀 크기 ${limitText(site)}`} />
            <div className="grid-2">
              <OptionCard selected={area === "GRID"} onClick={() => setArea("GRID")}>
                <span className="option-icon"><IconPackage size={32} /></span>
                <span className="option-title">일반</span>
                <span className="option-desc">입력구 틀에 들어가는 택배 · 보관함 1칸</span>
              </OptionCard>
              <OptionCard selected={area === "OVERSIZE"} onClick={() => setArea("OVERSIZE")}>
                <span className="option-icon"><IconOversize /></span>
                <span className="option-title">규격 초과</span>
                <span className="option-desc">{limitText(site)} 초과 · 배송 불가 보관함</span>
              </OptionCard>
            </div>

            <SectionLabel title="취급 주의" optional hint="깨지기 쉬운 물품" />
            <button type="button" className={`toggle-row ${fragile ? "on" : ""}`} onClick={() => setFragile((v) => !v)}
                    aria-pressed={fragile}>
              <span className="option-icon"><IconFragile /></span>
              <span className="toggle-text">
                <b>취급 주의 물품입니다</b>
                <small>로봇 배송 없이 직접 수령으로만 보관됩니다.</small>
              </span>
              <span className="switch"><span /></span>
            </button>

            <SectionLabel title="송장번호" optional hint="바코드 스캐너 입력 가능" />
            <label className={`field ${trackingError ? "invalid" : ""}`}>
              <input
                placeholder="영문·숫자 6~40자리" value={tracking} maxLength={40} autoComplete="off"
                onChange={(e) => setTracking(e.target.value.trim())}
                onKeyDown={(e) => {
                  // 바코드 스캐너는 글자 입력 직후 Enter 를 보내므로 입력창 값으로 판단
                  const value = e.currentTarget.value.trim();
                  if (e.key !== "Enter" || !area || (value && !TRACKING_RE.test(value))) return;
                  setTracking(value);
                  setStep(3);
                }}
              />
            </label>
            {trackingError
              ? <p className="field-error">{trackingError}</p>
              : <p className="field-help">송장번호가 없어도 접수할 수 있으며 접수번호는 서버에서 자동 생성됩니다.</p>}
          </Card>
        )}

        {site && step === 3 && area && (
          <Card>
            <Eyebrow>Parcel Intake</Eyebrow>
            <Title text="마지막으로 확인해 주세요" mark="." />
            <p className="subtitle">접수하면 보관함이 올라오거나 배송 불가 보관함 문이 열립니다.</p>

            <div className="summary">
              <div className="summary-head">
                <span className="summary-icon"><IconPackage /></span>
                <div>
                  <div className="summary-title">택배 접수 정보</div>
                  <div className="summary-sub">PARCEL INFORMATION</div>
                </div>
                <span className="badge">입력 완료</span>
              </div>
              <dl>
                <div className="summary-row"><dt>배송 목적지</dt><dd>{unitLabel(site, building, unit)}</dd></div>
                <div className="summary-row">
                  <dt>보관 구분</dt><dd>{area === "GRID" ? "일반 · 그리드 보관함" : "규격 초과 · 배송 불가 보관함"}</dd>
                </div>
                <div className="summary-row"><dt>취급 주의</dt><dd className={fragile ? "" : "empty"}>{fragile ? "예" : "아니요"}</dd></div>
                <div className="summary-row">
                  <dt>송장번호</dt>
                  <dd className={tracking ? "" : "empty"}>{tracking || "미입력"}</dd>
                </div>
              </dl>
            </div>
            {locked && <div className="alert alert-warn">취급 주의·규격 초과 물품은 로봇 배송 없이 직접 수령으로 보관됩니다.</div>}
            {!connected && <div className="alert alert-warn">서버에 연결되어 있지 않습니다. 연결되면 접수할 수 있습니다.</div>}
            {error && <div className="alert alert-error">{error}</div>}
          </Card>
        )}

        {site && step === 4 && receipt && (
          <Card className="insert">
            {(insert === "waiting" || insert === "confirming") && (
              <>
                {receipt.area === "GRID" ? (
                  <>
                    <h1 className="insert-port">입력구에 올라온 <em>보관함</em>에 넣어주세요</h1>
                    <p className="receipt-small">접수번호 <b>{receipt.no}</b></p>
                    <div className="ports">
                      <div className="port port-wide target">
                        <span className="port-arrow"><IconArrowDown /></span>
                        <IconPackage size={40} />
                        <small>그리드 입력구</small>
                      </div>
                    </div>
                  </>
                ) : (
                  <>
                    <h1 className="insert-port">배송 불가 보관함 <em>{pad2(receipt.door)}번</em> 문이 열렸습니다</h1>
                    <p className="receipt-small">물품을 넣고 문을 닫아 주세요 · 접수번호 <b>{receipt.no}</b></p>
                    <div className="ports doors">
                      {doorWindow(receipt.door ?? 1, site.oversize_doors).map((d) => (
                        <div key={d} className={`port door ${d === receipt.door ? "target" : ""}`}>
                          {d === receipt.door && <span className="port-arrow"><IconArrowDown /></span>}
                          <IconDoor />
                          {pad2(d)}
                        </div>
                      ))}
                    </div>
                  </>
                )}
                <div className="countdown">
                  <div className="countdown-track">
                    <div className={`countdown-fill ${remaining <= 15 ? "low" : ""}`}
                         style={{ width: `${(remaining / receipt.total) * 100}%` }} />
                  </div>
                  <div className="countdown-text">
                    {receipt.area === "GRID" ? "넣은 뒤 [투입 완료]를 눌러 주세요" : "문을 닫은 뒤 [보관 완료]를 눌러 주세요"}
                    {" "}· 남은 시간 {remaining}초
                  </div>
                </div>
                <div className="center-actions">
                  <PrimaryButton onClick={confirmInsert} disabled={insert === "confirming" || !connected}>
                    {insert === "confirming" ? "확인 중…" : receipt.area === "GRID" ? "투입 완료" : "보관 완료"}
                  </PrimaryButton>
                </div>
                {error && <div className="alert alert-error">{error}</div>}
              </>
            )}

            {insert === "done" && (
              <>
                <div className="done-check"><IconCheck size={48} /></div>
                <h1 className="done-title">등록 완료</h1>
                <div className="receipt-big"><span>접수번호</span><b>{receipt.no}</b></div>
                <p className="muted" style={{ marginTop: 22 }}>잠시 후 다음 물품 등록 화면으로 이동합니다.</p>
                <div className="center-actions">
                  <PrimaryButton onClick={nextItem}>다음 물품 등록</PrimaryButton>
                </div>
              </>
            )}

            {insert === "cancelled" && (
              <>
                <div className="done-check warn">!</div>
                <h1 className="done-title">접수가 취소되었습니다</h1>
                <p className="muted">{cancelReason}</p>
                <div className="center-actions">
                  <SecondaryButton onClick={resetAll}>처음으로</SecondaryButton>
                  <PrimaryButton onClick={retry}>다시 접수하기</PrimaryButton>
                </div>
              </>
            )}
          </Card>
        )}
      </main>

      <footer className="bottombar">
        <span className="bottombar-note"><IconInfo size={16} /> LastGate 보관소 입력 키오스크 · {client?.clientId ?? "연결 준비 중"}</span>
        <div className="bottombar-actions">
          {site && step === 1 && <PrimaryButton onClick={() => setStep(2)} disabled={!canNext1}>다음</PrimaryButton>}
          {site && step === 2 && (
            <>
              <SecondaryButton back onClick={() => setStep(1)}>이전</SecondaryButton>
              <PrimaryButton onClick={() => setStep(3)} disabled={!canNext2}>다음</PrimaryButton>
            </>
          )}
          {site && step === 3 && (
            <>
              <SecondaryButton back onClick={() => setStep(2)}>이전</SecondaryButton>
              <PrimaryButton onClick={submit} disabled={busy || !connected}>
                {busy ? "접수 중…" : "택배 접수"}
              </PrimaryButton>
            </>
          )}
        </div>
      </footer>

      {endOpen && (
        <div className="modal-backdrop" role="dialog" aria-modal="true">
          <div className="modal">
            <h2>업무를 종료할까요?</h2>
            <p className="muted">
              이 키오스크에서 오늘 {todayCount ?? 0}건이 등록되었습니다.<br />
              종료하면 입력 화면이 처음 상태로 돌아갑니다.
            </p>
            <div className="center-actions">
              <SecondaryButton onClick={() => setEndOpen(false)}>취소</SecondaryButton>
              <PrimaryButton arrow={false} onClick={() => { resetAll(); setEndOpen(false); }}>
                업무 종료
              </PrimaryButton>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/** 열린 문 주변 문 번호 최대 5개 (안내 그림용) */
function doorWindow(door: number, total: number) {
  const count = Math.min(5, Math.max(total, door));
  const start = Math.max(1, Math.min(door - 2, Math.max(total, door) - count + 1));
  return Array.from({ length: count }, (_, i) => start + i);
}
