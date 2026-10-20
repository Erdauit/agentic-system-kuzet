// «Оперативная обстановка» (SPEC.md §3.3): активные инциденты в реальном времени, карточка
// инцидента (кадр, ключевые кадры, клип ±15 с, доставки), действия оператора и звук тревоги.

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api, post } from "../../api/client";
import { connectRealtime } from "../../api/realtime";
import {
  mediaUrl,
  type CameraOut,
  type FrameRef,
  type IncidentOut,
  type QEvent,
  type ReasonCode,
} from "../../api/types";
import { LiveView } from "../../components/LiveView";
import type { MessageKey, T } from "../../i18n";

const POLL_MS = 15_000;
const REASONS: ReasonCode[] = ["steam", "welding", "repair_lying", "reflection", "other"];
const errText = (e: unknown) => (e instanceof ApiError ? e.message : String(e));
const time = (iso: string) => new Date(iso).toLocaleTimeString();

// Звук тревоги: три коротких сигнала через WebAudio. Браузер разрешает звук только после
// действия пользователя, поэтому оператор один раз нажимает «Включить звук» в начале смены.
class Siren {
  private ctx: AudioContext | null = null;

  get enabled(): boolean {
    return this.ctx !== null && this.ctx.state === "running";
  }

  async enable(): Promise<void> {
    this.ctx = this.ctx ?? new AudioContext();
    await this.ctx.resume();
  }

  disable(): void {
    void this.ctx?.close();
    this.ctx = null;
  }

  beep(critical: boolean): void {
    const ctx = this.ctx;
    if (!ctx || ctx.state !== "running") return;
    for (let i = 0; i < 3; i++) {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "square";
      osc.frequency.value = critical ? 880 : 660;
      gain.gain.value = 0.15;
      osc.connect(gain).connect(ctx.destination);
      const start = ctx.currentTime + i * 0.35;
      osc.start(start);
      osc.stop(start + 0.2);
    }
  }
}

function Frame({ frame, alt }: { frame: FrameRef; alt: string }) {
  const b = frame.bbox;
  return (
    <figure className="frame">
      <img src={mediaUrl(frame.blob_key)} alt={alt} loading="lazy" />
      {b && (
        <span
          className="bbox"
          style={{ left: `${b.x1 * 100}%`, top: `${b.y1 * 100}%`, width: `${(b.x2 - b.x1) * 100}%`, height: `${(b.y2 - b.y1) * 100}%` }}
        />
      )}
      <figcaption className="muted">{time(frame.captured_at)}</figcaption>
    </figure>
  );
}

function IncidentCard({ id, t, names, onClose }: { id: string; t: T; names: Record<string, string>; onClose: () => void }) {
  const [data, setData] = useState<IncidentOut | null>(null);
  const [reason, setReason] = useState<ReasonCode>("other");
  const [comment, setComment] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(() => {
    api<IncidentOut>(`/api/v1/incidents/${id}`).then(setData).catch((e: unknown) => setMsg(errText(e)));
  }, [id]);

  useEffect(() => {
    load();
    return connectRealtime((m) => {
      const inc = m["incident"] as { id?: string } | undefined;
      if (m["incident_id"] === id || inc?.id === id) load();
    });
  }, [id, load]);

  if (!data) return <div className="card">{msg ?? t("loading")}</div>;
  const inc = data.incident;
  const first: QEvent | undefined = data.events[0];
  const closed = inc.state === "RESOLVED" || inc.state === "FALSE_ALARM";
  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      setMsg(null);
      load();
    } catch (e) {
      setMsg(errText(e));
    }
  };

  return (
    <div className={`card incident sev-${inc.severity}`}>
      <div className="row between">
        <h2>
          {inc.synthetic === "selftest" && <span className="badge">{t("ops.selftest")}</span>} {t(`type.${inc.type}` as MessageKey)}
          {first?.routing === "event_with_arbiter" && <span className="muted"> ({t("ops.review")})</span>}
        </h2>
        <button type="button" className="link" onClick={onClose}>{t("ops.close")}</button>
      </div>
      <p>
        <span className={`status state-${inc.state}`}>{t(`state.${inc.state}` as MessageKey)}</span>{" "}
        {inc.camera_ids.map((c) => names[c] ?? c).join(", ")}
        {inc.area && <span className="muted"> · {inc.area}</span>} · {t("ops.opened")} {time(inc.opened_at)}
        {inc.escalation_level > 0 && <span className="bad"> · {t("ops.level")} {inc.escalation_level}</span>}
        {inc.acknowledged_by && <span className="muted"> · {t("ops.acked")}: {inc.acknowledged_by}</span>}
      </p>
      <div className="frames">
        {data.events.flatMap((e) => e.evidence.map((f) => <Frame key={f.blob_key} frame={f} alt={names[e.camera_id] ?? e.camera_id} />))}
      </div>
      <h3>{t("ops.clip")}</h3>
      {inc.clip ? (
        <video className="clip" src={mediaUrl(inc.clip.blob_key)} controls preload="metadata" />
      ) : (
        <p className="muted">{t("ops.clipPending")}</p>
      )}
      {inc.keyframes.length > 0 && (
        <>
          <h3>{t("ops.keyframes")}</h3>
          <div className="frames small">
            {inc.keyframes.map((f) => <Frame key={f.blob_key} frame={f} alt="" />)}
          </div>
        </>
      )}
      {!closed && (
        <div className="actions">
          {inc.state === "OPEN" && (
            <button type="button" onClick={() => void act(() => post(`/api/v1/incidents/${id}/ack`))}>{t("ops.ack")}</button>
          )}
          <button type="button" onClick={() => void act(() => post(`/api/v1/incidents/${id}/resolve`, { verdict: "confirmed", comment: comment || null }))}>
            {t("ops.confirm")}
          </button>
          <label>
            {t("ops.reason")}
            <select value={reason} onChange={(e) => setReason(e.target.value as ReasonCode)}>
              {REASONS.map((r) => <option key={r} value={r}>{t(`reason.${r}` as MessageKey)}</option>)}
            </select>
          </label>
          <label>
            {t("ops.comment")}
            <input value={comment} maxLength={1000} onChange={(e) => setComment(e.target.value)} />
          </label>
          <button type="button" className="danger"
            onClick={() => void act(() => post(`/api/v1/incidents/${id}/resolve`, { verdict: "false_alarm", reason_code: reason, comment: comment || null }))}>
            {t("ops.false")}
          </button>
        </div>
      )}
      {msg && <p className="bad">{msg}</p>}
      <details>
        <summary>{t("ops.deliveries")} ({data.deliveries.length})</summary>
        <table className="table">
          <tbody>
            {data.deliveries.map((d) => (
              <tr key={`${d.channel}-${d.target}-${d.level}`} className={d.status === "failed" ? "lvl-fail" : d.status === "retrying" ? "lvl-warn" : ""}>
                <td>{d.channel}</td>
                <td>{d.target}</td>
                <td>{t("ops.level")} {d.level}</td>
                <td>{d.status}{d.attempt > 1 ? ` ×${d.attempt}` : ""}</td>
                <td className="muted">{d.error ?? (d.sent_at ? time(d.sent_at) : "")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}

export function OperationsPage({ t }: { t: T }) {
  const [incidents, setIncidents] = useState<IncidentOut[]>([]);
  const [events, setEvents] = useState<QEvent[]>([]);
  const [cameras, setCameras] = useState<CameraOut[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [live, setLive] = useState<string | null>(null);
  const [banner, setBanner] = useState<{ title: string; text: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const siren = useRef(new Siren());
  const [sound, setSound] = useState(false);
  const [online, setOnline] = useState(true);

  const load = useCallback(() => {
    Promise.all([
      api<IncidentOut[]>("/api/v1/incidents?scope=active&synthetic=true"),
      api<QEvent[]>("/api/v1/events?limit=30"),
    ])
      .then(([inc, ev]) => {
        setIncidents(inc.filter((i) => i.incident.synthetic !== "canary"));
        setEvents(ev);
        setError(null);
      })
      .catch((e: unknown) => setError(errText(e)));
  }, []);

  useEffect(() => {
    load();
    api<CameraOut[]>("/api/v1/cameras").then(setCameras).catch(() => undefined);
    // Страховка на случай обрыва WebSocket: лента обновляется и опросом.
    const poll = window.setInterval(load, POLL_MS);
    const stop = connectRealtime((m) => {
      if (m.type === "incident" || m.type === "event") load();
      if (m.type === "alert") {
        setBanner({ title: String(m["title"] ?? ""), text: String(m["text"] ?? "") });
        if (m["sound"]) siren.current.beep(m["severity"] === "critical");
        if (typeof m["incident_id"] === "string") setOpen((cur) => cur ?? (m["incident_id"] as string));
      }
    }, setOnline);
    return () => {
      window.clearInterval(poll);
      stop();
    };
  }, [load]);

  const names = Object.fromEntries(cameras.map((c) => [c.camera.id, c.camera.name]));
  const toggleSound = async () => {
    if (siren.current.enabled) {
      siren.current.disable();
      setSound(false);
    } else {
      await siren.current.enable();
      siren.current.beep(false);
      setSound(true);
    }
  };

  return (
    <section>
      <div className="row between">
        <h1>{t("ops.title")}</h1>
        <button type="button" className={sound ? "" : "danger"} onClick={() => void toggleSound()}>
          {sound ? t("ops.soundOff") : t("ops.soundOn")}
        </button>
      </div>
      {banner && (
        <div className="alert banner" role="alert">
          <strong>{banner.title}</strong> {banner.text}
          <button type="button" className="link" onClick={() => setBanner(null)}>×</button>
        </div>
      )}
      {!online && <p className="alert">{t("ops.offline")}</p>}
      {error && <p className="alert">{error}</p>}
      {open && <IncidentCard id={open} t={t} names={names} onClose={() => setOpen(null)} />}
      <h2>{t("ops.active")}</h2>
      {incidents.length === 0 ? (
        <p className="muted">{t("ops.none")}</p>
      ) : (
        <ul className="incidents">
          {incidents.map(({ incident: i, events: ev }) => (
            <li key={i.id} className={`incident-row sev-${i.severity} state-${i.state}`}>
              <button type="button" className="link" onClick={() => setOpen(i.id)}>
                {i.synthetic === "selftest" && <span className="badge">{t("ops.selftest")}</span>} {t(`type.${i.type}` as MessageKey)} —{" "}
                {i.camera_ids.map((c) => names[c] ?? c).join(", ")}
              </button>
              <span className="muted">
                {time(i.opened_at)} · {t(`state.${i.state}` as MessageKey)}
                {i.escalation_level > 0 && ` · ${t("ops.level")} ${i.escalation_level}`}
                {ev.length > 1 && ` · ${ev.length}`}
              </span>
            </li>
          ))}
        </ul>
      )}
      <h2>{t("ops.cameras")}</h2>
      <div className="camera-grid">
        {cameras.filter((c) => c.camera.enabled).map((c) => (
          <div key={c.camera.id} className={`card cam-tile cstatus-${c.status}`}>
            <div className="row between">
              <strong>{c.camera.name}</strong>
              <span className={`status cs-${c.status}`}>{t(`cstatus.${c.status}` as MessageKey)}</span>
            </div>
            {live === c.camera.id ? (
              <LiveView cameraId={c.camera.id} onClose={() => setLive(null)} closeLabel={t("camera.stop")} />
            ) : (
              <img className="snapshot" src={`/api/v1/cameras/${c.camera.id}/snapshot`} alt={c.camera.name} loading="lazy"
                onError={(e) => ((e.target as HTMLImageElement).style.visibility = "hidden")} />
            )}
            {c.live_available && live !== c.camera.id && (
              <button type="button" className="link" onClick={() => setLive(c.camera.id)}>{t("camera.live")}</button>
            )}
          </div>
        ))}
      </div>
      <h2>{t("ops.events")}</h2>
      <table className="table">
        <tbody>
          {events.map((e) => (
            <tr key={e.id}>
              <td>{time(e.detected_at)}</td>
              <td>{t(`type.${e.type}` as MessageKey)}</td>
              <td>{names[e.camera_id] ?? e.camera_id}</td>
              <td className="muted">{Math.round(e.detector_score * 100)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
