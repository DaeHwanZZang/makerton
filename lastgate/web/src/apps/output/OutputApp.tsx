// 출력 키오스크 (세대인): 아이디/비밀번호 로그인 → 내 택배 목록(표) → [택배 수령]
import { useEffect, useRef, useState } from "react";
import { IDLE_LOGOUT_SEC, TOPICS, kioskId } from "../../shared/config";
import { useLocker, useNow } from "../../shared/hooks";
import { IconInfo } from "../../shared/icons";
import {
  Brand, Card, ConnectionBadge, Eyebrow, PrimaryButton, SecondaryButton, Title, Toast,
} from "../../shared/ui";

type Parcel = { receipt_no: string; size: string; arrived_at: string | null; mode: string };
type Household = { dong: number; ho: number; default_mode: string };
type ToastState = { kind: "ok" | "warn" | "error"; text: string } | null;

const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "-", "0", "⌫"];
const dateFormat = new Intl.DateTimeFormat("ko-KR", {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
});
const formatDate = (iso: string | null) => (iso ? dateFormat.format(new Date(iso)) : "-");

export default function OutputApp() {
  const { client, connected } = useLocker(kioskId("OUTPUT-01"));
  const [session, setSession] = useState<string | null>(null);
  const [household, setHousehold] = useState<Household | null>(null);
  const [parcels, setParcels] = useState<Parcel[]>([]);
  const [userId, setUserId] = useState("");
  const [password, setPassword] = useState("");
  const [field, setField] = useState<"id" | "pw">("id");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [taking, setTaking] = useState<string | null>(null);
  const [toast, setToast] = useState<ToastState>(null);
  const lastActive = useRef(Date.now());
  const householdRef = useRef<Household | null>(null);
  const now = useNow(500);

  householdRef.current = household;
  const idleLeft = Math.max(0, IDLE_LOGOUT_SEC - Math.floor((now - lastActive.current) / 1000));

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
    if (session && idleLeft === 0) logout("60초 동안 조작이 없어 자동 로그아웃되었습니다.");
  }, [session, idleLeft]);

  // 로봇 적재 등으로 목록이 바뀌면 서버가 PARCELS_UPDATE 를 보낸다
  useEffect(() => {
    if (!client) return;
    return client.onPush((msg) => {
      const h = householdRef.current;
      if (msg.type === "PARCELS_UPDATE" && h && msg.household?.dong === h.dong && msg.household?.ho === h.ho) {
        setParcels(msg.parcels);
      }
    });
  }, [client]);

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 4000);
    return () => window.clearTimeout(t);
  }, [toast]);

  async function login() {
    if (!client) return;
    setBusy(true);
    setError(null);
    try {
      const res = await client.request(TOPICS.login, { user_id: userId.trim(), password });
      if (res.status === "OK") {
        setSession(res.session);
        setHousehold(res.household);
        setParcels(res.parcels);
        setPassword("");
        lastActive.current = Date.now();
      } else {
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
    setUserId("");
    setPassword("");
    setField("id");
    setError(null);
    if (message) setToast({ kind: "warn", text: message });
  }

  async function take(parcel: Parcel) {
    if (!client || !session) return;
    setTaking(parcel.receipt_no);
    try {
      const res = await client.request(TOPICS.retrieve, { session, receipt_no: parcel.receipt_no });
      if (res.status === "EXPIRED_SESSION") {
        logout(res.message);
        return;
      }
      setParcels((list) => list.filter((p) => p.receipt_no !== parcel.receipt_no));
      setToast(res.status === "OK"
        ? { kind: "ok", text: `수령 출력구에서 택배를 꺼내 주세요 · ${parcel.receipt_no}` }
        : { kind: "warn", text: res.message ?? "수령할 수 없는 택배입니다." });
    } catch (e) {
      setToast({ kind: "error", text: (e as Error).message });
    } finally {
      setTaking(null);
    }
  }

  function press(key: string) {
    const set = field === "id" ? setUserId : setPassword;
    if (key === "⌫") set((v) => v.slice(0, -1));
    else set((v) => (v + key).slice(0, 20));
  }

  return (
    <div className="kiosk">
      <header className="topbar">
        <div className="topbar-left"><Brand /></div>
        <span className="topbar-title">택배 수령</span>
        <div className="topbar-right"><ConnectionBadge connected={connected} /></div>
      </header>

      <main className="main">
        {!session ? (
          <Card>
            <Eyebrow>Parcel Pickup</Eyebrow>
            <Title text="택배를 찾으러 오셨나요" mark="?" />
            <p className="subtitle">아이디와 비밀번호를 입력하면 보관 중인 택배를 확인할 수 있습니다.</p>
            <div className="login-grid">
              <div className="stack">
                <div>
                  <p className="field-label">아이디</p>
                  <label className={`field ${field === "id" ? "active" : ""}`}>
                    <input value={userId} placeholder="예: 101-412" autoComplete="off" inputMode="numeric"
                           onFocus={() => setField("id")}
                           onChange={(e) => setUserId(e.target.value.slice(0, 20))}
                           onKeyDown={(e) => e.key === "Enter" && setField("pw")} />
                  </label>
                  <p className="field-help">아이디는 '동-호수' 형식입니다.</p>
                </div>
                <div>
                  <p className="field-label">비밀번호</p>
                  <label className={`field ${field === "pw" ? "active" : ""}`}>
                    <input type="password" value={password} placeholder="비밀번호" autoComplete="off"
                           inputMode="numeric" onFocus={() => setField("pw")}
                           onChange={(e) => setPassword(e.target.value.slice(0, 20))}
                           onKeyDown={(e) => e.key === "Enter" && userId && password && login()} />
                  </label>
                </div>
                {error && <div className="alert alert-error">{error}</div>}
                {!connected && <div className="alert alert-warn">서버에 연결되어 있지 않습니다.</div>}
              </div>
              <div>
                <div className="keypad">
                  {KEYS.map((k) => (
                    <button key={k} type="button" className={`key ${k === "⌫" || k === "-" ? "key-fn" : ""}`}
                            onClick={() => press(k)}>
                      {k}
                    </button>
                  ))}
                </div>
                <button type="button" className="btn btn-ghost btn-sm" style={{ width: "100%", marginTop: 10 }}
                        onClick={() => (field === "id" ? setUserId("") : setPassword(""))}>
                  전체 지움
                </button>
              </div>
            </div>
          </Card>
        ) : (
          <Card>
            <Eyebrow>Parcel Pickup</Eyebrow>
            <div className="list-head">
              <div>
                <Title text={`${household?.dong}동 ${household?.ho}호`} mark="." />
                <p className="subtitle">
                  보관 중인 택배 {parcels.length}건 · [택배 수령]을 누르면 수령 출력구로 택배가 나옵니다.
                </p>
              </div>
            </div>
            {parcels.length === 0 ? (
              <div className="empty">보관 중인 택배가 없습니다.</div>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>접수번호</th>
                    <th>도착일</th>
                    <th className="col-action">수령</th>
                  </tr>
                </thead>
                <tbody>
                  {parcels.map((p) => (
                    <tr key={p.receipt_no}>
                      <td className="receipt-cell">{p.receipt_no}</td>
                      <td>{formatDate(p.arrived_at)}</td>
                      <td className="col-action">
                        <button type="button" className="btn btn-primary btn-take"
                                disabled={taking !== null || !connected} onClick={() => take(p)}>
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
          ) : (
            <PrimaryButton onClick={login} disabled={!userId || !password || busy || !connected}>
              {busy ? "확인 중…" : "로그인"}
            </PrimaryButton>
          )}
        </div>
      </footer>

      {toast && <Toast kind={toast.kind} text={toast.text} />}
    </div>
  );
}
