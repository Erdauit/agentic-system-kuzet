// Мастер, шаг 5: сценарии камеры, зоны и участки (рисуются кликами по снимку), калибровка пола
// по 4 точкам. Координаты — доли кадра (0..1), поэтому зоны не зависят от разрешения потока.

import { useCallback, useEffect, useRef, useState, type MouseEvent } from "react";

import { ApiError, api, patch, put } from "../../api/client";
import type { CalibrationOut, CameraOut, Point, Zone } from "../../api/types";
import type { MessageKey, T } from "../../i18n";

const SCENARIOS = ["fall", "red_zone", "smoke_fire"] as const;
const errText = (e: unknown) => (e instanceof ApiError ? e.message : String(e));
const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40);

function Overlay({ zones, draft, calib, onClick }: { zones: Zone[]; draft: Point[]; calib: Point[]; onClick: (p: Point) => void }) {
  const ref = useRef<SVGSVGElement>(null);
  const click = (e: MouseEvent<SVGSVGElement>) => {
    const r = ref.current?.getBoundingClientRect();
    if (!r) return;
    onClick({ x: Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1), y: Math.min(Math.max((e.clientY - r.top) / r.height, 0), 1) });
  };
  const pts = (p: Point[]) => p.map((q) => `${q.x * 100},${q.y * 100}`).join(" ");
  return (
    <svg ref={ref} className="overlay" viewBox="0 0 100 100" preserveAspectRatio="none" onClick={click}>
      {zones.map((z) => (
        <polygon key={z.id} points={pts(z.polygon.points)} className={`zone-${z.kind}`} />
      ))}
      {draft.length > 0 && <polyline points={pts(draft)} className="zone-draft" />}
      {draft.map((p, i) => <circle key={i} cx={p.x * 100} cy={p.y * 100} r={0.8} className="zone-draft" />)}
      {calib.length > 0 && <polygon points={pts(calib)} className="calib" />}
      {calib.map((p, i) => (
        <text key={i} x={p.x * 100 + 1} y={p.y * 100 - 1} className="calib-label">{i + 1}</text>
      ))}
    </svg>
  );
}

export function ZonesStep({ t }: { t: T }) {
  const [cameras, setCameras] = useState<CameraOut[]>([]);
  const [camId, setCamId] = useState<string>("");
  const [zones, setZones] = useState<Zone[]>([]);
  const [mode, setMode] = useState<"zone" | "calib">("zone");
  const [draft, setDraft] = useState<Point[]>([]);
  const [calib, setCalib] = useState<Point[]>([]);
  const [meters, setMeters] = useState("");
  const [form, setForm] = useState({ name: "", kind: "monitoring" as Zone["kind"], area: "", dwell: "" });
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    api<CameraOut[]>("/api/v1/cameras").then((c) => {
      setCameras(c);
      setCamId((cur) => cur || (c[0]?.camera.id ?? ""));
    }).catch((e: unknown) => setMsg(errText(e)));
  }, []);

  const load = useCallback(() => {
    if (!camId) return;
    api<Zone[]>(`/api/v1/cameras/${camId}/zones`).then(setZones).catch((e: unknown) => setMsg(errText(e)));
    api<CalibrationOut>(`/api/v1/cameras/${camId}/calibration`).then((c) => {
      setCalib(c.calibration?.image_points ?? []);
      setMeters(c.calibration?.known_distance ? String(c.calibration.known_distance.meters) : "");
    }).catch(() => setCalib([]));
    setDraft([]);
  }, [camId]);
  useEffect(load, [load]);

  const cam = cameras.find((c) => c.camera.id === camId);
  const saveZones = async (next: Zone[]) => {
    try {
      setZones(await put<Zone[]>(`/api/v1/cameras/${camId}/zones`, { zones: next }));
      setMsg(t("saved"));
    } catch (e) {
      setMsg(errText(e));
    }
  };
  const addZone = async () => {
    if (draft.length < 3 || !form.name) return;
    const id = `${camId}-${slug(form.name) || "zone"}-${Date.now().toString(36)}`.slice(0, 64);
    const zone: Zone = {
      id,
      camera_id: camId,
      name: form.name,
      kind: form.kind,
      polygon: { points: draft },
      dwell_seconds: form.kind === "red_zone" && form.dwell ? Number(form.dwell) : null,
      work_schedule: [],
      area: form.area ? slug(form.area) : null,
    };
    await saveZones([...zones, zone]);
    setDraft([]);
    setForm({ ...form, name: "" });
  };
  const saveCalib = async () => {
    try {
      await put<CalibrationOut>(`/api/v1/cameras/${camId}/calibration`, {
        image_points: calib,
        known_distance: meters ? { point_a: 0, point_b: 1, meters: Number(meters) } : null,
      });
      setMsg(t("zones.calibrated"));
    } catch (e) {
      setMsg(errText(e));
    }
  };
  const toggleScenario = async (s: string, on: boolean) => {
    if (!cam) return;
    const next = on ? [...new Set([...cam.camera.scenarios, s])] : cam.camera.scenarios.filter((x) => x !== s);
    try {
      const updated = await patch<CameraOut>(`/api/v1/cameras/${camId}`, { scenarios: next });
      setCameras(cameras.map((c) => (c.camera.id === camId ? updated : c)));
    } catch (e) {
      setMsg(errText(e));
    }
  };
  const onPoint = (p: Point) => {
    if (mode === "zone") setDraft([...draft, p]);
    else setCalib(calib.length >= 4 ? [p] : [...calib, p]);
  };

  return (
    <div className="card">
      <label>
        {t("zones.camera")}
        <select value={camId} onChange={(e) => setCamId(e.target.value)}>
          {cameras.map((c) => <option key={c.camera.id} value={c.camera.id}>{c.camera.name}</option>)}
        </select>
      </label>
      {cam && (
        <fieldset>
          <legend>{t("zones.scenarios")}</legend>
          {SCENARIOS.map((s) => (
            <label key={s} className="inline">
              <input type="checkbox" checked={cam.camera.scenarios.includes(s)} onChange={(e) => void toggleScenario(s, e.target.checked)} />
              {t(`type.${s}` as MessageKey)}
            </label>
          ))}
        </fieldset>
      )}
      <div className="tabs" role="tablist">
        <button type="button" role="tab" aria-selected={mode === "zone"} onClick={() => setMode("zone")}>{t("zones.mode.zone")}</button>
        <button type="button" role="tab" aria-selected={mode === "calib"} onClick={() => setMode("calib")}>{t("zones.mode.calib")}</button>
      </div>
      <p className="muted">{mode === "zone" ? t("zones.draw") : t("zones.calibHint")}</p>
      {camId && (
        <div className="editor">
          <img src={`/api/v1/cameras/${camId}/snapshot`} alt={cam?.camera.name ?? camId}
            onError={() => setMsg(t("zones.noSnapshot"))} />
          <Overlay zones={zones} draft={mode === "zone" ? draft : []} calib={mode === "calib" ? calib : []} onClick={onPoint} />
        </div>
      )}
      {mode === "zone" ? (
        <>
          <div className="row">
            <input placeholder={t("zones.name")} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value as Zone["kind"] })}>
              <option value="monitoring">{t("zones.monitoring")}</option>
              <option value="red_zone">{t("zones.red")}</option>
            </select>
          </div>
          <label>
            {t("zones.area")}
            <input value={form.area} placeholder="line-3" onChange={(e) => setForm({ ...form, area: e.target.value })} />
          </label>
          {form.kind === "red_zone" && (
            <label>
              {t("zones.dwell")}
              <input type="number" min={0.1} step={0.1} value={form.dwell} onChange={(e) => setForm({ ...form, dwell: e.target.value })} />
            </label>
          )}
          <div className="row">
            <button type="button" disabled={draft.length < 3 || !form.name} onClick={() => void addZone()}>{t("zones.addZone")}</button>
            <button type="button" className="link" onClick={() => setDraft([])}>{t("zones.reset")}</button>
          </div>
          <table className="table">
            <tbody>
              {zones.map((z) => (
                <tr key={z.id}>
                  <td>{z.name}</td>
                  <td>{z.kind === "red_zone" ? t("zones.red") : t("zones.monitoring")}</td>
                  <td className="muted">{z.area ?? "—"}</td>
                  <td>
                    <button type="button" className="link" onClick={() => void saveZones(zones.filter((x) => x.id !== z.id))}>{t("zones.delete")}</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <>
          <label>
            {t("zones.meters")}
            <input type="number" min={0.1} step={0.1} value={meters} onChange={(e) => setMeters(e.target.value)} />
          </label>
          <div className="row">
            <button type="button" disabled={calib.length !== 4} onClick={() => void saveCalib()}>{t("save")}</button>
            <button type="button" className="link" onClick={() => setCalib([])}>{t("zones.reset")}</button>
          </div>
        </>
      )}
      {msg && <p className="muted">{msg}</p>}
    </div>
  );
}
