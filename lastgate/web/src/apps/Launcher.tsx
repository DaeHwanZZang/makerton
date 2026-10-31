// 시작 화면: 키오스크 선택 + 보관소 상태(서버 retained summary) 모니터
import { useEffect, useState } from "react";
import { SIZES } from "../shared/config";
import { useLocker } from "../shared/hooks";
import type { Msg } from "../shared/mqtt";
import { IconPackage, IconSizeM } from "../shared/icons";
import { Brand, Card, ConnectionBadge, Eyebrow, Title } from "../shared/ui";

export default function Launcher() {
  const { client, connected } = useLocker("MONITOR");
  const [summary, setSummary] = useState<Msg | null>(null);

  useEffect(() => {
    if (!client) return;
    return client.onSummary(setSummary);
  }, [client]);

  const jobs: { job_id: string; status: string }[] = summary?.active_jobs ?? [];

  return (
    <div className="kiosk">
      <header className="topbar">
        <div className="topbar-left"><Brand /></div>
        <span className="topbar-title">키오스크 선택</span>
        <div className="topbar-right"><ConnectionBadge connected={connected} /></div>
      </header>
      <main className="main">
        <Card>
          <Eyebrow>LastGate Locker</Eyebrow>
          <Title text="실행할 키오스크를 선택하세요" mark="." />
          <div className="launcher-grid">
            <a className="launch-card" href="/input">
              <span className="option-icon"><IconPackage size={34} /></span>
              <h2>입력 키오스크</h2>
              <p>택배기사 · 물품 접수</p>
            </a>
            <a className="launch-card" href="/output">
              <span className="option-icon"><IconSizeM size={34} /></span>
              <h2>출력 키오스크</h2>
              <p>세대인 · 로그인 후 택배 수령</p>
            </a>
          </div>

          <div className="section-label"><span className="section-title">보관소 상태</span>
            <span className="section-hint">{summary ? `갱신 ${new Date(summary.timestamp).toLocaleTimeString("ko-KR")}` : "서버 상태 수신 대기 중"}</span>
          </div>
          <div className="status-grid">
            {SIZES.map((s) => {
              const o = summary?.occupancy?.[s.key];
              return (
                <div key={s.key} className="stat">
                  <div className="stat-label">{s.key} · {s.label} 셀 사용</div>
                  <div className="stat-value">{o ? `${o.used} / ${o.total}` : "-"}</div>
                  <div className="bar"><span style={{ width: `${(o?.ratio ?? 0) * 100}%` }} /></div>
                </div>
              );
            })}
            <div className="stat">
              <div className="stat-label">로봇 배달 대기</div>
              <div className="stat-value">{summary?.waiting?.ROBOT ?? "-"}</div>
            </div>
            <div className="stat">
              <div className="stat-label">직접 수령 대기</div>
              <div className="stat-value">{summary?.waiting?.DIRECT ?? "-"}</div>
            </div>
            <div className="stat">
              <div className="stat-label">진행 중 Job</div>
              <div className="stat-value" style={{ fontSize: 18 }}>
                {jobs.length ? jobs.map((j) => `${j.job_id} (${j.status})`).join(", ") : "없음"}
              </div>
            </div>
          </div>
        </Card>
      </main>
    </div>
  );
}
