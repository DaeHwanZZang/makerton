// 입력 키오스크 (택배기사): 배송 주소 → 택배 정보 → 접수 확인 → 투입 안내(최소 정보)
import { useEffect, useRef, useState } from "react";
import {
  DONGS, SIZES, TOPICS, TRACKING_RE, kioskId, sizeLabel, validateHo, type SizeKey,
} from "../../shared/config";
import { useLocker, useNow, useTodayCount } from "../../shared/hooks";
import { IconArrowDown, IconBuilding, IconCheck, IconInfo, IconPackage, sizeIcon } from "../../shared/icons";
import {
  Brand, Card, ConnectionBadge, Eyebrow, InfoNote, OptionCard, PrimaryButton, SecondaryButton,
  SectionLabel, Stepper, Title,
} from "../../shared/ui";

type Step = 1 | 2 | 3 | 4;
type InsertState = "waiting" | "confirming" | "done" | "cancelled";
type Receipt = { no: string; port: SizeKey; deadline: number; total: number };

const STEPS = ["배송 주소", "택배 정보", "접수 확인"];
const NEXT_ITEM_DELAY_MS = 3000;

export default function InputApp() {
  const kiosk = kioskId("INPUT-01");
  const { client, connected } = useLocker(kiosk);
  const [step, setStep] = useState<Step>(1);
  const [dong, setDong] = useState<number | null>(null);
  const [ho, setHo] = useState("");
  const [size, setSize] = useState<SizeKey | null>(null);
  const [tracking, setTracking] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [insert, setInsert] = useState<InsertState>("waiting");
  const [cancelReason, setCancelReason] = useState("");
  const [todayCount, applyTodayCount] = useTodayCount(client, connected, kiosk);
  const [endOpen, setEndOpen] = useState(false);
  const hoRef = useRef<HTMLInputElement>(null);
  const receiptRef = useRef<string | null>(null);
  const now = useNow();

  receiptRef.current = receipt?.no ?? null;
  const hoError = ho ? validateHo(ho) : null;
  const trackingError = tracking && !TRACKING_RE.test(tracking) ? "송장번호는 영문·숫자 6~40자리입니다." : null;
  const canNext1 = dong !== null && ho !== "" && !hoError;
  const canNext2 = size !== null && !trackingError;
  const remaining = receipt ? Math.max(0, Math.ceil((receipt.deadline - now) / 1000)) : 0;

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
    if (step === 1) hoRef.current?.focus();
  }, [step]);

  function nextItem() {
    // 같은 동에 연속 배송하는 경우가 많으므로 동은 유지
    setHo("");
    setSize(null);
    setTracking("");
    setReceipt(null);
    setInsert("waiting");
    setError(null);
    setStep(1);
  }

  function resetAll() {
    nextItem();
    setDong(null);
  }

  function retry() {
    setReceipt(null);
    setInsert("waiting");
    setError(null);
    setStep(3);
  }

  async function submit() {
    if (!client || !size || dong === null) return;
    setBusy(true);
    setError(null);
    try {
      const res = await client.request(TOPICS.register, {
        dong, ho: Number(ho), size, tracking_no: tracking || null, kiosk_id: kiosk,
      });
      if (res.status === "OK") {
        const total = Number(res.timeout_sec) || 60;
        setReceipt({ no: res.receipt_no, port: res.input_port, deadline: Date.now() + total * 1000, total });
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
        receipt_no: receipt.no, port: receipt.port, source: "KIOSK",
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
        {step === 1 && (
          <Card>
            <Eyebrow>Parcel Intake</Eyebrow>
            <Title text="어디로 보내시나요" mark="?" />
            <p className="subtitle">택배가 배송될 동과 호수를 선택해 주세요.</p>

            <SectionLabel title="아파트 동 선택" required hint={`총 ${DONGS.length}개 동`} />
            <div className="grid-3">
              {DONGS.map((d) => (
                <OptionCard key={d} selected={dong === d} onClick={() => setDong(d)}>
                  <span className="option-icon"><IconBuilding /></span>
                  <span className="option-title">{d}동</span>
                </OptionCard>
              ))}
            </div>

            <SectionLabel title="호수 입력" required hint="숫자만 입력" />
            <label className={`field ${hoError ? "invalid" : ""}`}>
              <input
                ref={hoRef} inputMode="numeric" placeholder="예: 412" value={ho} maxLength={4}
                onChange={(e) => setHo(e.target.value.replace(/\D/g, ""))}
                onKeyDown={(e) => {
                  // 상태 대신 입력창 값으로 판단 (입력 직후 Enter 가 와도 안전)
                  const value = e.currentTarget.value;
                  if (e.key === "Enter" && dong !== null && value && !validateHo(value)) setStep(2);
                }}
              />
              <span className="field-suffix">호</span>
            </label>
            {hoError
              ? <p className="field-error">{hoError}</p>
              : <p className="field-help">동·호수가 정확하지 않으면 택배가 잘못 분류될 수 있습니다. (101호 ~ 412호)</p>}

            <InfoNote>수취인 이름·전화번호는 수집하지 않습니다.</InfoNote>
          </Card>
        )}

        {step === 2 && (
          <Card>
            <Eyebrow>Parcel Intake</Eyebrow>
            <Title text="택배 정보를 알려주세요" mark="." />
            <p className="subtitle">보관 위치를 정하는 데 사용할 크기와 송장번호를 입력합니다.</p>

            <SectionLabel title="택배 크기" required hint="해당하는 크기를 선택" />
            <div className="grid-3">
              {SIZES.map((s) => (
                <OptionCard key={s.key} selected={size === s.key} onClick={() => setSize(s.key)}>
                  <span className="option-icon">{sizeIcon(s.key)}</span>
                  <span className="option-key">{s.key}</span>
                  <span className="option-title">{s.label}</span>
                  <span className="option-desc">{s.desc} · {s.limit}</span>
                </OptionCard>
              ))}
            </div>

            <SectionLabel title="송장번호" optional hint="바코드 스캐너 입력 가능" />
            <label className={`field ${trackingError ? "invalid" : ""}`}>
              <input
                placeholder="영문·숫자 6~40자리" value={tracking} maxLength={40} autoComplete="off"
                onChange={(e) => setTracking(e.target.value.trim())}
                onKeyDown={(e) => {
                  // 바코드 스캐너는 글자 입력 직후 Enter 를 보내므로 입력창 값으로 판단
                  const value = e.currentTarget.value.trim();
                  if (e.key !== "Enter" || !size || (value && !TRACKING_RE.test(value))) return;
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

        {step === 3 && dong !== null && size && (
          <Card>
            <Eyebrow>Parcel Intake</Eyebrow>
            <Title text="마지막으로 확인해 주세요" mark="." />
            <p className="subtitle">접수 후 화면에 표시되는 입력구에 택배를 넣어 주세요.</p>

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
                <div className="summary-row"><dt>배송 목적지</dt><dd>{dong}동 {ho}호</dd></div>
                <div className="summary-row"><dt>택배 크기</dt><dd>{size} · {sizeLabel(size)}</dd></div>
                <div className="summary-row">
                  <dt>송장번호</dt>
                  <dd className={tracking ? "" : "empty"}>{tracking || "미입력"}</dd>
                </div>
              </dl>
            </div>
            {!connected && <div className="alert alert-warn">서버에 연결되어 있지 않습니다. 연결되면 접수할 수 있습니다.</div>}
            {error && <div className="alert alert-error">{error}</div>}
          </Card>
        )}

        {step === 4 && receipt && (
          <Card className="insert">
            {(insert === "waiting" || insert === "confirming") && (
              <>
                <h1 className="insert-port"><em>{receipt.port}</em> 입력구에 넣어주세요</h1>
                <p className="receipt-small">접수번호 <b>{receipt.no}</b></p>
                <div className="ports">
                  {SIZES.map((s) => (
                    <div key={s.key} className={`port ${s.key === receipt.port ? "target" : ""}`}>
                      {s.key === receipt.port && <span className="port-arrow"><IconArrowDown /></span>}
                      {s.key}
                      <small>{s.label}</small>
                    </div>
                  ))}
                </div>
                <div className="countdown">
                  <div className="countdown-track">
                    <div className={`countdown-fill ${remaining <= 15 ? "low" : ""}`}
                         style={{ width: `${(remaining / receipt.total) * 100}%` }} />
                  </div>
                  <div className="countdown-text">넣은 뒤 [투입 완료]를 눌러 주세요 · 남은 시간 {remaining}초</div>
                </div>
                <div className="center-actions">
                  <PrimaryButton onClick={confirmInsert} disabled={insert === "confirming" || !connected}>
                    {insert === "confirming" ? "확인 중…" : "투입 완료"}
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
          {step === 1 && <PrimaryButton onClick={() => setStep(2)} disabled={!canNext1}>다음</PrimaryButton>}
          {step === 2 && (
            <>
              <SecondaryButton back onClick={() => setStep(1)}>이전</SecondaryButton>
              <PrimaryButton onClick={() => setStep(3)} disabled={!canNext2}>다음</PrimaryButton>
            </>
          )}
          {step === 3 && (
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
