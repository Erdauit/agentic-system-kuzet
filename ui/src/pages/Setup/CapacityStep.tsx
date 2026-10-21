// Мастер, шаг 6: план мощности по замеру `qoima bench` — FPS камер, загрузка, VRAM, серая зона,
// применение (только человеком) и PDF-отчёт.

import { useCallback, useEffect, useState } from "react";

import { ApiError, api, post } from "../../api/client";
import type { BenchSummary, CapacityPlan, CapacityStatus, StoredPlan } from "../../api/types";
import { CapacityBanner } from "../../components/CapacityBanner";
import type { MessageKey, T } from "../../i18n";

const errText = (e: unknown) => (e instanceof ApiError ? e.message : String(e));
const pct = (x: number) => `${Math.round(x * 100)}%`;

function Usage({ plan, t }: { plan: CapacityPlan; t: T }) {
  return (
    <table className="table">
      <tbody>
        {plan.utilization.map((u) => {
          const ratio = u.capacity > 0 ? u.used / u.capacity : 0;
          const label = u.unit === "доля" ? `${pct(u.used)} / ${pct(u.capacity)}` : `${Math.round(u.used)} / ${Math.round(u.capacity)} ${u.unit}`;
          return (
            <tr key={u.resource}>
              <td>{u.resource}</td>
              <td style={{ width: "50%" }}>
                <div className="meter"><span className={ratio > 1 ? "over" : ratio > 0.9 ? "high" : ""} style={{ width: `${Math.min(ratio, 1) * 100}%` }} /></div>
              </td>
              <td>{label}{!u.measured && <span className="muted"> ({t("cap.notMeasured")})</span>}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function PlanView({ plan, t }: { plan: CapacityPlan; t: T }) {
  return (
    <>
      <p>
        <span className={`status verdict-${plan.verdict}`}>{t(`cap.verdict.${plan.verdict}` as MessageKey)}</span>{" "}
        <span className="muted">{plan.benchmark_id} · {new Date(plan.created_at).toLocaleString()}</span>
      </p>
      <h3>{t("cap.usage")}</h3>
      <Usage plan={plan} t={t} />
      <h3>{t("cap.cameras")}</h3>
      <table className="table">
        <tbody>
          {plan.allocations.map((a) => (
            <tr key={`${a.camera_id}-${a.scenario}`} className={a.fps_active < a.target_fps ? "lvl-warn" : ""}>
              <td>{a.camera_id}</td>
              <td>{t(`type.${a.scenario}` as MessageKey)}</td>
              <td>{a.fps_active} / {a.target_fps} {t("cap.fps")}</td>
              <td className="muted">{a.gpu_index === null ? "CPU" : `GPU${a.gpu_index}`}</td>
            </tr>
          ))}
          {plan.allocations.length === 0 && <tr><td className="muted">{t("cap.noAllocations")}</td></tr>}
        </tbody>
      </table>
      {plan.vram.length > 0 && (
        <>
          <h3>VRAM</h3>
          <table className="table">
            <tbody>
              {plan.vram.map((v) => (
                <tr key={v.gpu_index}>
                  <td>GPU{v.gpu_index}</td>
                  <td>{t("cap.vramLine")}: {v.pipeline_mb} · Triton {v.triton_mb} · VLM {v.llm_mb} · {t("cap.training")} {v.worker_mb} · {t("cap.reserve")} {v.reserve_mb} / {v.total_mb} МБ</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      <h3>{t("cap.vlm")}</h3>
      <p>
        {plan.vlm
          ? `${plan.vlm.expected_rps.toFixed(3)} / ${(plan.vlm.measured_rps * plan.vlm.max_load_ratio).toFixed(2)} ${t("cap.rps")} · gpu_memory_utilization ${plan.llm_gpu_memory_utilization}`
          : t("cap.noVlm")}
      </p>
      <p className="muted">
        {Object.entries(plan.gray_zone).map(([s, z]) => `${t(`type.${s}` as MessageKey)}: ${z.t_low}–${z.t_high}`).join(" · ")}
      </p>
      {plan.warnings.length > 0 && (
        <ul className="warnings">
          {plan.warnings.map((w) => <li key={w}>{w}</li>)}
        </ul>
      )}
    </>
  );
}

export function CapacityStep({ t }: { t: T }) {
  const [bench, setBench] = useState<BenchSummary[]>([]);
  const [status, setStatus] = useState<CapacityStatus | null>(null);
  const [draft, setDraft] = useState<StoredPlan | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);

  const load = useCallback(() => {
    api<BenchSummary[]>("/api/v1/capacity/bench").then(setBench).catch((e: unknown) => setMsg(errText(e)));
    api<CapacityStatus>("/api/v1/capacity/status").then(setStatus).catch((e: unknown) => setMsg(errText(e)));
  }, []);
  useEffect(load, [load]);

  const compute = async () => {
    setBusy(true);
    try {
      setDraft(await post<StoredPlan>("/api/v1/capacity/plans", { bench_id: bench[0]?.id ?? null }));
      setMsg(null);
    } catch (e) {
      setMsg(errText(e));
    } finally {
      setBusy(false);
    }
  };
  const apply = async (id: string) => {
    setBusy(true);
    try {
      await post(`/api/v1/capacity/plans/${id}/apply`);
      setDraft(null);
      setMsg(t("cap.applied"));
      setRefresh((x) => x + 1);
      load();
    } catch (e) {
      setMsg(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const active = status?.active ?? null;
  const shown = draft?.plan ?? null;
  return (
    <div className="card">
      <p>{t("cap.hint")}</p>
      <pre className="cmd">qoima bench</pre>
      <CapacityBanner t={t} refreshKey={refresh} />
      {bench.length === 0 ? (
        <p className="alert">{t("cap.noBench")}</p>
      ) : (
        <p className="muted">
          {t("cap.lastBench")}: {bench[0]?.id} · {bench[0]?.hostname} · {bench[0]?.gpus.join(", ") || "CPU"} · {t("cap.models")}: {bench[0]?.models.join(", ") || "—"}
        </p>
      )}
      <div className="row">
        <button type="button" disabled={busy || bench.length === 0} onClick={() => void compute()}>{t("cap.compute")}</button>
        {active && <a className="button-link" href={`/api/v1/capacity/plans/${active.plan.id}/report.pdf`}>{t("cap.pdf")}</a>}
      </div>
      {msg && <p className="muted">{msg}</p>}
      {shown && (
        <div className="card">
          <h3>{t("cap.draft")}</h3>
          <PlanView plan={shown} t={t} />
          <div className="row">
            <button type="button" disabled={busy || shown.verdict === "does_not_fit" || draft?.status === "active"} onClick={() => void apply(shown.id)}>
              {draft?.status === "active" ? t("cap.alreadyActive") : t("cap.apply")}
            </button>
            <a className="button-link" href={`/api/v1/capacity/plans/${shown.id}/report.pdf`}>{t("cap.pdf")}</a>
          </div>
        </div>
      )}
      {active && !shown && (
        <div className="card">
          <h3>{t("cap.active")} <span className="muted">({active.applied_by}, {active.applied_at && new Date(active.applied_at).toLocaleString()})</span></h3>
          <PlanView plan={active.plan} t={t} />
        </div>
      )}
    </div>
  );
}
