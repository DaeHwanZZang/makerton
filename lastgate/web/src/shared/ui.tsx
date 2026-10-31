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
