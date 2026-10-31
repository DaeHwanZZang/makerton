// 출력 키오스크 (세대인): 건물 선택 + 호실 + 비밀번호 로그인 → 내 택배 목록 → [택배 수령] / [전체 수령]
// 그리드 보관함은 출력구로 하나씩 올라오고, 배송 불가 보관함은 문이 한꺼번에 열린다.
import { useEffect, useRef, useState } from "react";
import { IDLE_LOGOUT_SEC, TOPICS, checkUnit, keyLeadsToUnit, kioskId } from "../../shared/config";
import { useLocker, useNow, useSite } from "../../shared/hooks";
import { IconBuilding, IconDoor, IconInfo, IconPackage } from "../../shared/icons";
import type { LockerClient, Msg } from "../../shared/mqtt";
import {
  Badge, Brand, Card, ConnectionBadge, Eyebrow, Keypad, OptionCard, PrimaryButton, SecondaryButton,
  SiteGate, Title, Toast, applyKey,
} from "../../shared/ui";

type Parcel = {
  receipt_no: string; arrived_at: string | null; mode: string; status: string; storage_area: "GRID" | "OVERSIZE";
  space_id: string | null; door: number | null; fragile: boolean; direct_lock: string | null; plan: string | null;
};
type Household = { building: number; building_name: string; unit: number; label: string; default_mode: string };
type ToastState = { kind: "ok" | "warn" | "error"; text: string } | null;
// 전체 수령 진행: 문 열린 배송 불가 보관함 + 그리드 보관함 순서
type Batch = { doors: number[]; grid: Parcel[]; index: number; busy: boolean; error: string | null };

const dateFormat = new Intl.DateTimeFormat("ko-KR", {
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
});
const formatDate = (iso: string | null) => (iso ? dateFormat.format(new Date(iso)) : "-");
const pad2 = (n: number | null) => String(n ?? 0).padStart(2, "0");
const LOCK_TEXT: Record<string, string> = { FRAGILE: "취급 주의", OVERSIZE: "규격 초과", FAILED: "배달 실패" };

export default function OutputApp() {
  const { client, connected } = useLocker(kioskId("OUTPUT-01"));
  const [session, setSession] = useState<string | null>(null);
  const [household, setHousehold] = useState<Household | null>(null);
  const [parcels, setParcels] = useState<Parcel[]>([]);
  const [building, setBuilding] = useState<number | null>(null);
  const [unit, setUnit] = useState("");
  const [password, setPassword] = useState("");
  const [field, setField] = useState<"unit" | "pw">("unit");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [taking, setTaking] = useState<string | null>(null);
  const [batch, setBatch] = useState<Batch | null>(null);
  const [toast, setToast] = useState<ToastState>(null);
  const lastActive = useRef(Date.now());
  const householdRef = useRef<Household | null>(null);
  const now = useNow(500);
  const { site, loaded } = useSite(client, !session && unit === "" && password === "");

  householdRef.current = household;
  const buildings = site?.buildings ?? [];
  const current = buildings.find((b) => b.code === building) ?? null;
  const unitTerm = site?.unit_term ?? "호";
  const unitCheck = checkUnit(current, unitTerm, unit);
  const idleLeft = Math.max(0, IDLE_LOGOUT_SEC - Math.floor((now - lastActive.current) / 1000));

  useEffect(() => {
    if (buildings.length === 1 && building !== buildings[0].code) setBuilding(buildings[0].code);
  }, [site]);

  useEffect(() => {
    const touch = () => { lastActive.current = Date.now(); };
    window.addEventListener("pointerdown", touch);
    window.addEventListener("keydown", touch);
    return () => {
      window.removeEventListener("pointerdown", touch);
      window.removeEventListener("keydown", touch);
    };
  }, []);

  useEffect(() => {
    if (session && idleLeft === 0 && !batch) logout("60초 동안 조작이 없어 자동 로그아웃되었습니다.");
  }, [session, idleLeft, batch]);

  // 로봇 적재 등으로 목록이 바뀌면 서버가 PARCELS_UPDATE 를 보낸다
  useEffect(() => {
    if (!client) return;
    return client.onPush((msg) => {
      const h = householdRef.current;
      if (msg.type === "PARCELS_UPDATE" && h && msg.household?.building === h.building && msg.household?.unit === h.unit) {
        setParcels(msg.parcels);
      }
    });
  }, [client]);

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 4500);
    return () => window.clearTimeout(t);
  }, [toast]);

  async function login() {
    if (!client || building === null) return;
    setBusy(true);
    setError(null);
    try {
      const res = await client.request(TOPICS.login, { building, unit: Number(unit), password });
      if (res.status === "OK") {
        setSession(res.session);
        setHousehold(res.household);
        setParcels(res.parcels);
        setPassword("");
        lastActive.current = Date.now();
        if (res.must_change) setToast({ kind: "warn", text: "초기 비밀번호를 쓰고 있습니다. 세대 웹 ⚙ 설정에서 바꿔 주세요." });
      } else {
        setPassword("");
        setError(res.message ?? "로그인할 수 없습니다.");
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function logout(message?: string) {
    if (client && session) client.request(TOPICS.logout, { session }).catch(() => undefined);
    setSession(null);
    setHousehold(null);
    setParcels([]);
    setBatch(null);
    setUnit("");
    setPassword("");
    setField("unit");
    setError(null);
    if (buildings.length !== 1) setBuilding(null);
    if (message) setToast({ kind: "warn", text: message });
  }

  /** 한 건 꺼내기. 성공하면 서버 응답, 실패하면 오류 문구 */
  async function retrieve(c: LockerClient, parcel: Parcel): Promise<Msg | string> {
    const res = await c.request(TOPICS.retrieve, { session, receipt_no: parcel.receipt_no });
    if (res.status === "EXPIRED_SESSION") {
      logout(res.message);
      return res.message;
    }
    setParcels((list) => list.filter((p) => p.receipt_no !== parcel.receipt_no));
    return res.status === "OK" ? res : res.message ?? "수령할 수 없는 택배입니다.";
  }

  async function take(parcel: Parcel) {
    if (!client || !session) return;
    setTaking(parcel.receipt_no);
    try {
      const res = await retrieve(client, parcel);
      if (typeof res === "string") setToast({ kind: "warn", text: res });
      else if (res.storage_area === "OVERSIZE") setToast({ kind: "ok", text: `배송 불가 보관함 ${pad2(res.door)}번 문이 열렸습니다` });
      else setToast({ kind: "ok", text: "출력구에 보관함이 올라왔습니다 · 택배를 꺼내 주세요" });
    } catch (e) {
      setToast({ kind: "error", text: (e as Error).message });
    } finally {
      setTaking(null);
    }
  }

  async function takeAll() {
    if (!client || !session || parcels.length === 0) return;
    const oversize = parcels.filter((p) => p.storage_area === "OVERSIZE");
    const grid = parcels.filter((p) => p.storage_area !== "OVERSIZE");
    setBatch({ doors: [], grid, index: -1, busy: true, error: null });
    const doors: number[] = [];
    let failed: string | null = null;
    for (const p of oversize) {       // 배송 불가 보관함은 한꺼번에 연다
      try {
        const res = await retrieve(client, p);
        if (typeof res === "string") failed = res;
        else doors.push(res.door);
      } catch (e) {
        failed = (e as Error).message;
      }
    }
    setBatch({ doors, grid, index: -1, busy: false, error: failed });
    if (grid.length) await nextGrid({ doors, grid, index: -1, busy: false, error: failed });
  }

  async function nextGrid(state: Batch) {
    if (!client) return;
    const index = state.index + 1;
    if (index >= state.grid.length) {
      setBatch(null);
      setToast({ kind: "ok", text: "모든 택배를 꺼냈습니다." });
      return;
    }
    setBatch({ ...state, index, busy: true, error: null });
    try {
      const res = await retrieve(client, state.grid[index]);
      setBatch({ ...state, index, busy: false, error: typeof res === "string" ? res : null });
    } catch (e) {
      setBatch({ ...state, index, busy: false, error: (e as Error).message });
    }
  }

  function press(key: string) {
    if (field === "unit") setUnit((v) => applyKey(v, key, 4));
    else setPassword((v) => applyKey(v, key, 8));
  }

  const canLogin = current !== null && unitCheck.state === "ok" && password.length >= 4 && !busy && connected;

  return (
    <div className="kiosk">
      <header className="topbar">
        <div className="topbar-left"><Brand /></div>
        <span className="topbar-title">택배 수령</span>
        <div className="topbar-right"><ConnectionBadge connected={connected} /></div>
      </header>

      <main className="main">
        {!site && <SiteGate loaded={loaded} connected={connected} />}

        {site && !session && (
          <Card>
            <Eyebrow>Parcel Pickup · {site.name}</Eyebrow>
            <Title text="택배를 찾으러 오셨나요" mark="?" />
            <p className="subtitle">{site.building_term}·{unitTerm}과 비밀번호를 입력하면 보관 중인 택배를 확인할 수 있습니다.</p>
            {buildings.length > 1 && (
              <div className="building-chips">
                {buildings.map((b) => (
                  <OptionCard key={b.code} selected={building === b.code}
                              onClick={() => { setBuilding(b.code); setUnit(""); setField("unit"); }}>
                    <span className="option-icon"><IconBuilding size={22} /></span>
                    <span className="option-title">{b.name}</span>
                  </OptionCard>
                ))}
              </div>
            )}
            <div className="login-grid">
              <div className="stack">
                <div>
                  <p className="field-label">{unitTerm}</p>
                  <label className={`field ${field === "unit" ? "active" : ""} ${unitCheck.state === "error" ? "invalid" : ""}`}>
                    <input value={unit} placeholder={current ? `예: ${current.units[0]}` : `먼저 ${site.building_term} 선택`}
                           autoComplete="off" inputMode="numeric" disabled={!current}
                           onFocus={() => setField("unit")}
                           onChange={(e) => setUnit(e.target.value.replace(/\D/g, "").slice(0, 4))}
                           onKeyDown={(e) => e.key === "Enter" && setField("pw")} />
                    <span className="field-suffix">{unitTerm}</span>
                  </label>
                  {current && (unitCheck.state === "error"
                    ? <p className="field-error">{unitCheck.message}</p>
                    : <p className="field-help">{current.name} · {current.guide}</p>)}
                </div>
                <div>
                  <p className="field-label">비밀번호</p>
                  <label className={`field ${field === "pw" ? "active" : ""}`}>
                    <input type="password" value={password} placeholder="숫자 4~8자리" autoComplete="off"
                           inputMode="numeric" onFocus={() => setField("pw")}
                           onChange={(e) => setPassword(e.target.value.replace(/\D/g, "").slice(0, 8))}
                           onKeyDown={(e) => e.key === "Enter" && canLogin && login()} />
                  </label>
                </div>
                {error && <div className="alert alert-error">{error}</div>}
                {!connected && <div className="alert alert-warn">서버에 연결되어 있지 않습니다.</div>}
              </div>
              <div>
                <Keypad onKey={press}
                        enabled={field === "unit" ? (d) => !!current && keyLeadsToUnit(current.units, unit, d) : undefined} />
                <p className="field-help" style={{ textAlign: "center" }}>
                  {field === "unit" ? `${unitTerm} 입력 중` : "비밀번호 입력 중"}
                </p>
              </div>
            </div>
          </Card>
        )}

        {site && session && (
          <Card>
            <Eyebrow>Parcel Pickup</Eyebrow>
            <div className="list-head">
              <div>
                <Title text={household?.label ?? ""} mark="." />
                <p className="subtitle">보관 중인 택배 {parcels.length}건 · 로봇에 싣기 전이면 배송 예정 택배도 꺼낼 수 있습니다.</p>
              </div>
              {parcels.length > 1 && (
                <PrimaryButton onClick={takeAll} disabled={taking !== null || batch !== null || !connected}>
                  전체 수령 ({parcels.length})
                </PrimaryButton>
              )}
            </div>
            {parcels.length === 0 ? (
              <div className="empty">보관 중인 택배가 없습니다.</div>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>접수번호</th>
                    <th>도착</th>
                    <th>보관 위치</th>
                    <th>수령 방식</th>
                    <th className="col-action">수령</th>
                  </tr>
                </thead>
                <tbody>
                  {parcels.map((p) => (
                    <tr key={p.receipt_no}>
                      <td className="receipt-cell">{p.receipt_no}</td>
                      <td>{formatDate(p.arrived_at)}</td>
                      <td>{p.storage_area === "OVERSIZE" ? `배송 불가 보관함 ${pad2(p.door)}번` : "그리드 보관함"}</td>
                      <td>
                        {p.mode === "DIRECT" ? "직접 수령" : p.status === "JOB_ASSIGNED" ? "로봇 출발 준비" : "로봇 배송 예정"}
                        {p.direct_lock && <> <Badge tone="warn">{LOCK_TEXT[p.direct_lock] ?? "직접 수령"}</Badge></>}
                        {!p.direct_lock && p.fragile && <> <Badge tone="warn">취급 주의</Badge></>}
                      </td>
                      <td className="col-action">
                        <button type="button" className="btn btn-primary btn-take"
                                disabled={taking !== null || batch !== null || !connected} onClick={() => take(p)}>
                          {taking === p.receipt_no ? "출력 중…" : "택배 수령"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        )}
      </main>

      <footer className="bottombar">
        <span className="bottombar-note">
          <IconInfo size={16} />
          {session ? `${idleLeft}초 동안 조작이 없으면 자동 로그아웃됩니다` : `LastGate 보관소 출력 키오스크 · ${client?.clientId ?? "연결 준비 중"}`}
        </span>
        <div className="bottombar-actions">
          {session ? (
            <SecondaryButton onClick={() => logout()}>로그아웃</SecondaryButton>
          ) : site && (
            <PrimaryButton onClick={login} disabled={!canLogin}>
              {busy ? "확인 중…" : "로그인"}
            </PrimaryButton>
          )}
        </div>
      </footer>

      {batch && (
        <div className="modal-backdrop" role="dialog" aria-modal="true">
          <div className="modal modal-wide">
            {batch.doors.length > 0 && (
              <div className="batch-doors">
                <IconDoor />
                <b>배송 불가 보관함 {batch.doors.map(pad2).join(", ")}번 문이 열렸습니다</b>
              </div>
            )}
            {batch.index >= 0 && batch.grid.length > 0 ? (
              <>
                <div className="done-check"><IconPackage size={44} /></div>
                <h2>{batch.index + 1}/{batch.grid.length} 보관함이 {batch.busy ? "올라오는 중…" : "올라왔습니다"}</h2>
                <p className="muted">출력구의 보관함에서 택배를 꺼낸 뒤 [다음]을 눌러 주세요.</p>
              </>
            ) : (
              <h2>{batch.busy ? "문을 여는 중…" : batch.grid.length ? "보관함을 준비하는 중…" : "물품을 꺼낸 뒤 문을 닫아 주세요"}</h2>
            )}
            {batch.error && <div className="alert alert-warn">{batch.error}</div>}
            <div className="center-actions">
              {batch.grid.length > 0 && batch.index >= 0 ? (
                <PrimaryButton onClick={() => nextGrid(batch)} disabled={batch.busy}>
                  {batch.index + 1 < batch.grid.length ? "다음" : "완료"}
                </PrimaryButton>
              ) : batch.grid.length === 0 && !batch.busy && (
                <PrimaryButton arrow={false} onClick={() => setBatch(null)}>완료</PrimaryButton>
              )}
            </div>
          </div>
        </div>
      )}

      {toast && <Toast kind={toast.kind} text={toast.text} />}
    </div>
  );
}
