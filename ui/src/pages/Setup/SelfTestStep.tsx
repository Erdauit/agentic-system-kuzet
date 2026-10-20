// Мастер, шаг 8: сквозной самотест. Тестовое событие на выбранных камерах проходит весь путь —
// кадр и клип из GOP-буфера, инцидент, алерт во все каналы. Человек подтверждает получение.
// Здесь же — канарейка пути алерта (результат последней проверки и запуск вручную).

import { useEffect, useState } from "react";

import { ApiError, api, post } from "../../api/client";
import type { CameraOut, SelfTestStatus } from "../../api/types";
import type { T } from "../../i18n";

const errText = (e: unknown) => (e instanceof ApiError ? e.message : String(e));
const mark = (ok: boolean) => <span className={ok ? "ok" : "muted"}>{ok ? "✓" : "…"}</span>;

interface Canary {
  enabled: boolean;
  interval_minutes: number;
  last: { ok: boolean | null; camera_id?: string; at?: string; seconds?: number; stage?: string; reason?: string } | null;
}

export function SelfTestStep({ t }: { t: T }) {
  const [cameras, setCameras] = useState<CameraOut[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [run, setRun] = useState<SelfTestStatus | null>(null);
  const [canary, setCanary] = useState<Canary | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    api<CameraOut[]>("/api/v1/cameras").then((c) => {
      const on = c.filter((x) => x.camera.enabled);
      setCameras(on);
      setSelected(on.slice(0, 2).map((x) => x.camera.id));
    }).catch((e: unknown) => setMsg(errText(e)));
    api<Canary>("/api/v1/setup/canary").then(setCanary).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!run || run.confirmed_at || (run.all_delivered && run.clip_ready)) return;
    const timer = window.setInterval(() => {
      api<SelfTestStatus>(`/api/v1/setup/selftest/${run.id}`).then(setRun).catch(() => undefined);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [run]);

  const start = async () => {
    try {
      setRun(await post<SelfTestStatus>("/api/v1/setup/selftest", { camera_ids: selected }));
      setMsg(null);
    } catch (e) {
      setMsg(errText(e));
    }
  };
  const confirm = async () => {
    if (!run) return;
    try {
      setRun(await post<SelfTestStatus>(`/api/v1/setup/selftest/${run.id}/confirm`));
    } catch (e) {
      setMsg(errText(e));
    }
  };
  const runCanary = async () => {
    try {
      const last = await post<Canary["last"]>("/api/v1/setup/canary/run");
      setCanary((c) => (c ? { ...c, last } : c));
    } catch (e) {
      setMsg(errText(e));
    }
  };

  return (
    <div className="card">
      <p>{t("selftest.hint")}</p>
      {cameras.map((c) => (
        <label key={c.camera.id} className="inline">
          <input type="checkbox" checked={selected.includes(c.camera.id)}
            onChange={(e) => setSelected(e.target.checked ? [...selected, c.camera.id] : selected.filter((x) => x !== c.camera.id))} />
          {c.camera.name}
        </label>
      ))}
      <button type="button" disabled={selected.length === 0} onClick={() => void start()}>{t("selftest.start")}</button>
      {run && (
        <>
          <table className="table">
            <tbody>
              {run.cameras.map((c) => (
                <tr key={c.camera_id}>
                  <td>{cameras.find((x) => x.camera.id === c.camera_id)?.camera.name ?? c.camera_id}</td>
                  <td>{mark(c.event_received)} {t("selftest.event")}</td>
                  <td>{mark(c.evidence)} {t("ops.keyframes")}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {run.incidents.map((i) => (
            <div key={i.incident_id} className="card">
              <p>
                {i.camera_ids.join(", ")} {i.area && <span className="muted">· {i.area}</span>} · {mark(Boolean(i.clip))} {t("selftest.clip")} ·{" "}
                {i.first_alert_seconds === null ? (
                  <span className="muted">{t("selftest.waiting")}</span>
                ) : (
                  <span className={i.within_slo ? "ok" : "bad"}>
                    {i.first_alert_seconds.toFixed(2)} с {t("selftest.latency")} ({t("health.target")} ≤ {run.slo_seconds} с)
                  </span>
                )}
              </p>
              <ul>
                {i.deliveries.map((d) => (
                  <li key={`${d.channel}-${d.target}`} className={d.status === "sent" ? "ok" : d.status === "failed" ? "bad" : "muted"}>
                    {d.channel} → {d.target}: {d.status}{d.error ? ` (${d.error})` : ""}
                  </li>
                ))}
              </ul>
            </div>
          ))}
          {run.confirmed_at ? (
            <p className="ok">{t("selftest.confirmed")}: {run.confirmed_by}</p>
          ) : (
            <button type="button" disabled={run.incidents.length === 0} onClick={() => void confirm()}>{t("selftest.confirm")}</button>
          )}
        </>
      )}
      <h3>{t("selftest.canary")}</h3>
      {canary && (
        <p className="muted">
          {canary.enabled ? `${canary.interval_minutes} мин` : "off"} ·{" "}
          {canary.last === null ? "—" : canary.last.ok ? <span className="ok">OK {canary.last.seconds} с</span> : <span className="bad">{canary.last.stage ?? canary.last.reason}</span>}
        </p>
      )}
      <button type="button" onClick={() => void runCanary()}>{t("selftest.canaryRun")}</button>
      {msg && <p className="bad">{msg}</p>}
    </div>
  );
}
