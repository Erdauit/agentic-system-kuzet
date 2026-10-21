import { useCallback, useEffect, useState } from "react";

import { api, post } from "../../api/client";
import { connectRealtime } from "../../api/realtime";
import { CapacityBanner } from "../../components/CapacityBanner";
import { isAdmin, type CameraOut, type HealthStatus, type SloRow, type SystemAlert, type SystemHealth, type User } from "../../api/types";
import { LiveView } from "../../components/LiveView";
import { Sparkline } from "../../components/Sparkline";
import type { Lang, MessageKey, T } from "../../i18n";

// Резервный опрос раз в 5 с (интервал Camera Health); основное обновление — по WebSocket.
const REFRESH_MS = 5000;

function StatusBadge({ status, t }: { status: HealthStatus; t: T }) {
  return <span className={`status status-${status}`}>{t(`status.${status}` as MessageKey)}</span>;
}

const CAMERA_TO_HEALTH: Record<string, HealthStatus> = { ok: "green", degraded: "yellow", down: "red", unknown: "unknown" };

function CameraCard({ cam, t }: { cam: CameraOut; t: T }) {
  const [live, setLive] = useState(false);
  const [history, setHistory] = useState<[number, number][]>([]);
  const h = cam.health;
  const color = CAMERA_TO_HEALTH[cam.status] ?? "unknown";

  useEffect(() => {
    api<{ points: [number, number][] }>(`/api/v1/cameras/${cam.camera.id}/history?metric=fps&range=24h`)
      .then((r) => setHistory(r.points))
      .catch(() => setHistory([]));
  }, [cam.camera.id]);

  return (
    <li className={`component component-${color}`}>
      <div className="component-head">
        <span className={`dot dot-${color}`} aria-hidden="true" />
        <strong>{cam.camera.name}</strong>
        <span className={`status status-${color}`}>{t(`cstatus.${cam.status}` as MessageKey)}</span>
      </div>
      {live ? (
        <LiveView cameraId={cam.camera.id} onClose={() => setLive(false)} closeLabel={t("camera.stop")} />
      ) : (
        <img className="snapshot" src={`/api/v1/cameras/${cam.camera.id}/snapshot`} alt={cam.camera.name} loading="lazy" />
      )}
      {h && h.issues.length > 0 && <p className="component-message">{h.issues.join(", ")}</p>}
      <p className="muted">
        {cam.camera.id} · {t("camera.fps")} {h ? h.fps_in.toFixed(1) : "—"}
        {h?.sharpness_ratio != null && ` · ${t("camera.sharpness")} ${(h.sharpness_ratio * 100).toFixed(0)}%`}
        {h?.brightness != null && ` · ${t("camera.brightness")} ${h.brightness.toFixed(0)}`}
        {h && ` · ${t("camera.reconnects")} ${h.reconnects_total}`}
      </p>
      <div className="row">
        <span className="muted">{t("camera.history")}</span>
        <Sparkline points={history} />
        {cam.live_available ? (
          !live && (
            <button type="button" onClick={() => setLive(true)}>
              {t("camera.live")}
            </button>
          )
        ) : (
          <span className="muted">{t("camera.noLive")}</span>
        )}
      </div>
    </li>
  );
}

export function HealthPage({ lang, t, user }: { lang: Lang; t: T; user: User }) {
  const [data, setData] = useState<SystemHealth | null>(null);
  const [cameras, setCameras] = useState<CameraOut[]>([]);
  const [slo, setSlo] = useState<SloRow[]>([]);
  const [alerts, setAlerts] = useState<SystemAlert[]>([]);
  const [error, setError] = useState(false);
  const admin = isAdmin(user);

  const load = useCallback(async () => {
    try {
      const [h, c, s] = await Promise.all([
        api<SystemHealth>("/api/v1/health"),
        api<CameraOut[]>("/api/v1/cameras"),
        api<SloRow[]>("/api/v1/slo"),
      ]);
      setData(h);
      setCameras(c);
      setSlo(s);
      if (admin) setAlerts(await api<SystemAlert[]>("/api/v1/system-alerts"));
      setError(false);
    } catch {
      // «Тишина ≠ норма»: недоступный core-api показываем явно, а не старыми данными.
      setError(true);
    }
  }, [admin]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    // Снимки камер приходят пачкой раз в 5 с — перезагружаем не чаще раза в секунду.
    let pending: number | undefined;
    const stop = connectRealtime((m) => {
      if ((m.type === "system_alert" || m.type === "camera_health") && pending === undefined) {
        pending = window.setTimeout(() => {
          pending = undefined;
          void load();
        }, 1000);
      }
    });
    return () => {
      window.clearInterval(timer);
      window.clearTimeout(pending);
      stop();
    };
  }, [load]);

  const ack = async (id: string) => {
    await post(`/api/v1/system-alerts/${id}/ack`);
    void load();
  };

  return (
    <section>
      <h1>{t("health.title")}</h1>
      {error && (
        <p className="alert" role="alert">
          {t("health.unreachable")}
        </p>
      )}
      {!data && !error && <p>{t("loading")}</p>}
      {data && (
        <p className="overall">
          {t("health.overall")}: <StatusBadge status={error ? "unknown" : data.status} t={t} />
          <span className="muted">
            {" "}
            · {t("health.updated")} {new Date(data.generated_at).toLocaleTimeString(lang)}
          </span>
        </p>
      )}

      {admin && (
        <>
          <h2>{t("health.alerts")}</h2>
          {alerts.length === 0 ? (
            <p className="muted">{t("health.noAlerts")}</p>
          ) : (
            <ul className="alerts">
              {alerts.map((a) => (
                <li key={a.id} className={`alert-item sev-${a.severity}`}>
                  <span className="muted">{new Date(a.raised_at).toLocaleTimeString(lang)}</span> {a.message}{" "}
                  {a.acknowledged_by ? (
                    <span className="muted">
                      ({t("health.acked")}: {a.acknowledged_by})
                    </span>
                  ) : (
                    <button type="button" onClick={() => void ack(a.id)}>
                      {t("health.ack")}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      )}

      <h2>{t("health.cameras")}</h2>
      {cameras.length === 0 ? (
        <p className="muted">{t("camera.none")}</p>
      ) : (
        <ul className="components">
          {cameras.filter((c) => c.camera.enabled).map((c) => (
            <CameraCard key={c.camera.id} cam={c} t={t} />
          ))}
        </ul>
      )}

      <CapacityBanner t={t} />
      <h2>{t("health.slo")}</h2>
      <table className="table">
        <tbody>
          {slo.map((r) => (
            <tr key={r.name}>
              <td>{r.title}</td>
              <td>
                {t("health.target")} ≤ {r.target_s} с
              </td>
              <td>
                {r.value_s == null ? (
                  <span className="muted">
                    {t("health.notMeasured")}
                    {r.note ? ` — ${r.note}` : ""}
                  </span>
                ) : (
                  <span className={r.status === "ok" ? "ok" : "bad"}>{r.value_s.toFixed(1)} с</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {data && (
        <>
          <h2>{t("health.components")}</h2>
          <ul className="components">
            {data.components.map((c) => (
              <li key={c.component} className={`component component-${c.status}`}>
                <div className="component-head">
                  <span className={`dot dot-${c.status}`} aria-hidden="true" />
                  <strong>{c.component}</strong>
                  <StatusBadge status={c.status} t={t} />
                  {c.latency_ms !== null && (
                    <span className="muted">
                      {Math.round(c.latency_ms)} {t("health.latency")}
                    </span>
                  )}
                </div>
                <p className="component-message">{c.message}</p>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
