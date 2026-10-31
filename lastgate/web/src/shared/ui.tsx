import type { ReactNode } from "react";
import { IconArrowLeft, IconArrowRight, IconCheck, IconInfo } from "./icons";

export function Eyebrow({ children }: { children: ReactNode }) {
  return (
    <div className="eyebrow">
      <span className="eyebrow-bar" />
      {children}
    </div>
  );
}

/** 큰 제목. 마지막 문장부호만 브랜드 색 */
export function Title({ text, mark }: { text: string; mark: string }) {
  return (
    <h1 className="title">
      {text}
      <span className="title-mark">{mark}</span>
    </h1>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`card ${className}`}>{children}</section>;
}

export function InfoNote({ children }: { children: ReactNode }) {
  return (
    <div className="info-note">
      <IconInfo />
      <span>{children}</span>
    </div>
  );
}

export function SectionLabel({ title, required, optional, hint }: {
  title: string; required?: boolean; optional?: boolean; hint?: string;
}) {
  return (
    <div className="section-label">
      <span className="section-title">
        {title}
        {required && <span className="required">*</span>}
        {optional && <span className="optional">선택</span>}
      </span>
      {hint && <span className="section-hint">{hint}</span>}
    </div>
  );
}

export function OptionCard({ selected, onClick, children }: {
  selected: boolean; onClick: () => void; children: ReactNode;
}) {
  return (
    <button type="button" className={`option ${selected ? "selected" : ""}`} onClick={onClick}>
      {selected && (
        <span className="option-check">
          <IconCheck size={16} />
        </span>
      )}
      {children}
    </button>
  );
}

export function PrimaryButton({ children, onClick, disabled, arrow = true }: {
  children: ReactNode; onClick: () => void; disabled?: boolean; arrow?: boolean;
}) {
  return (
    <button type="button" className="btn btn-primary" onClick={onClick} disabled={disabled}>
      {children}
      {arrow && <IconArrowRight />}
    </button>
  );
}

export function SecondaryButton({ children, onClick, back }: {
  children: ReactNode; onClick: () => void; back?: boolean;
}) {
  return (
    <button type="button" className="btn btn-secondary" onClick={onClick}>
      {back && <IconArrowLeft />}
      {children}
    </button>
  );
}

export function ConnectionBadge({ connected }: { connected: boolean }) {
  return (
    <span className={`conn ${connected ? "on" : "off"}`}>
      <span className="conn-dot" />
      {connected ? "서버 연결됨" : "서버 연결 끊김"}
    </span>
  );
}

const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "C", "0", "⌫"];

/**
 * 숫자 키패드. enabled(digit) 가 false 인 숫자 키는 끈다 (없는 호실로만 이어지는 키).
 * C = 전체 지움, ⌫ = 한 글자 지움.
 */
export function Keypad({ onKey, enabled }: { onKey: (key: string) => void; enabled?: (digit: string) => boolean }) {
  return (
    <div className="keypad">
      {KEYS.map((k) => {
        const fn = k === "C" || k === "⌫";
        return (
          <button key={k} type="button" className={`key ${fn ? "key-fn" : ""}`}
                  disabled={!fn && enabled ? !enabled(k) : false} onClick={() => onKey(k)}
                  aria-label={k === "C" ? "전체 지움" : k === "⌫" ? "한 글자 지움" : k}>
            {k === "C" ? "전체 지움" : k}
          </button>
        );
      })}
    </div>
  );
}

/** 키패드 입력을 문자열에 적용 */
export const applyKey = (value: string, key: string, max: number) =>
  key === "C" ? "" : key === "⌫" ? value.slice(0, -1) : (value + key).slice(0, max);

/** 관리자 초기 설정 전·설정 수신 전 화면 */
export function SiteGate({ loaded, connected }: { loaded: boolean; connected: boolean }) {
  return (
    <Card className="insert">
      <div className="done-check warn">!</div>
      <h1 className="done-title">{loaded ? "관리자 초기 설정이 필요합니다" : "설정을 불러오는 중입니다"}</h1>
      <p className="muted">
        {loaded
          ? "관리자 웹에서 건물·호실과 보관함 수를 설정하면 이 화면이 자동으로 바뀝니다."
          : connected ? "잠시만 기다려 주세요." : "보관소 서버에 연결되면 설정을 불러옵니다."}
      </p>
    </Card>
  );
}

export function Badge({ tone = "brand", children }: { tone?: "brand" | "warn" | "muted" | "danger"; children: ReactNode }) {
  return <span className={`tag tag-${tone}`}>{children}</span>;
}

export function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark">L</span>
      <span className="brand-name">LastGate</span>
    </div>
  );
}

export function Stepper({ steps, current }: { steps: string[]; current: number }) {
  return (
    <ol className="stepper">
      {steps.map((label, i) => {
        const n = i + 1;
        const state = n < current ? "done" : n === current ? "active" : "todo";
        return (
          <li key={label} className={`step ${state}`}>
            <span className="step-dot">{state === "done" ? <IconCheck size={16} /> : String(n).padStart(2, "0")}</span>
            <span className="step-label">{label}</span>
          </li>
        );
      })}
    </ol>
  );
}

export function Toast({ kind, text }: { kind: "ok" | "warn" | "error"; text: string }) {
  return <div className={`toast toast-${kind}`}>{text}</div>;
}
