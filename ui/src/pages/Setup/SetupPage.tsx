import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { ApiError, api, patch, post, put } from "../../api/client";
import { connectRealtime } from "../../api/realtime";
import type { AddResult, CameraOut, Delivery, DiscoveryJob, HardwareOut, SiteSettings } from "../../api/types";
import type { MessageKey, T } from "../../i18n";
import { SelfTestStep } from "./SelfTestStep";
import { ZonesStep } from "./ZonesStep";

type Step = "site" | "hardware" | "cameras" | "zones" | "alerts" | "selftest";

const STEPS: { id: Step; title: MessageKey }[] = [
  { id: "site", title: "setup.step1" },
  { id: "hardware", title: "setup.step2" },
  { id: "cameras", title: "setup.step3" },
  { id: "zones", title: "setup.step5" },
  { id: "alerts", title: "setup.step7" },
  { id: "selftest", title: "setup.step8" },
];

const errText = (e: unknown) => (e instanceof ApiError ? e.message : String(e));

// ---------------------------------------------------------------- шаг 1

function SiteStep({ t }: { t: T }) {
  const [site, setSite] = useState<SiteSettings | null>(null);
  const [shifts, setShifts] = useState("");
  const [subnets, setSubnets] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    api<SiteSettings>("/api/v1/setup/site").then((s) => {
      setSite(s);
      setShifts(s.shifts.map((x) => `${x.name} ${x.start}-${x.end}`).join("\n"));
      setSubnets(s.discovery_subnets.join(", "));
    }).catch((e: unknown) => setMsg(errText(e)));
  }, []);

  if (!site) return <p>{msg ?? t("loading")}</p>;

  const save = async (e: FormEvent) => {
    e.preventDefault();
    const parsed = shifts.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
      const m = l.match(/^(.+?)\s+(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})$/);
      return m ? { name: m[1] ?? "", start: m[2] ?? "", end: m[3] ?? "" } : { name: l, start: "", end: "" };
    });
    try {
      await put("/api/v1/setup/site", {
        ...site,
        shifts: parsed,
        discovery_subnets: subnets.split(",").map((s) => s.trim()).filter(Boolean),
      });
      setMsg(t("saved"));
    } catch (err) {
      setMsg(errText(err));
    }
  };

  return (
    <form className="card" onSubmit={(e) => void save(e)}>
      <label>
        {t("site.name")}
        <input value={site.name} onChange={(e) => setSite({ ...site, name: e.target.value })} required />
      </label>
      <label>
        {t("site.timezone")}
        <input value={site.timezone} onChange={(e) => setSite({ ...site, timezone: e.target.value })} required />
      </label>
      <label>
        {t("site.language")}
        <select value={site.default_language} onChange={(e) => setSite({ ...site, default_language: e.target.value as "ru" | "kk" })}>
          <option value="ru">Русский</option>
          <option value="kk">Қазақша</option>
        </select>
      </label>
      <label>
        {t("site.shifts")} <span className="muted">(«Дневная 08:00-20:00», по строке на смену)</span>
        <textarea rows={3} value={shifts} onChange={(e) => setShifts(e.target.value)} />
      </label>
      <label>
        {t("site.subnets")}
        <input value={subnets} onChange={(e) => setSubnets(e.target.value)} placeholder="10.0.5.0/24, 10.0.6.0/24" />
      </label>
      {msg && <p className="muted">{msg}</p>}
      <button type="submit">{t("save")}</button>
    </form>
  );
}

// ---------------------------------------------------------------- шаг 2

function HardwareStep({ t }: { t: T }) {
  const [hw, setHw] = useState<HardwareOut | null>(null);
  useEffect(() => {
    api<HardwareOut>("/api/v1/setup/hardware").then(setHw).catch(() => setHw({ report: null }));
  }, []);
  if (!hw) return <p>{t("loading")}</p>;
  if (!hw.report) return <p className="alert">{hw.message ?? t("hw.none")}</p>;
  return (
    <div className="card">
      <p>
        {t("hw.age")}: {hw.report.hostname}, {hw.age_minutes} {t("hw.minAgo")}
        {hw.stale && <strong className="bad"> ({t("hw.stale")})</strong>}
      </p>
      <table className="table">
        <tbody>
          {hw.report.outcomes.map((o) => (
            <tr key={o.name} className={`lvl-${o.level}`}>
              <td>{o.name}</td>
              <td>{o.level.toUpperCase()}</td>
              <td>{o.detail}</td>
              <td className="muted">{o.recommendation}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------- шаг 3

function CamerasStep({ t }: { t: T }) {
  const [creds, setCreds] = useState([{ username: "admin", password: "" }]);
  const [job, setJob] = useState<DiscoveryJob | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [results, setResults] = useState<AddResult[]>([]);
  const [cameras, setCameras] = useState<CameraOut[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [manual, setManual] = useState({ name: "", main_url: "", sub_url: "", username: "", password: "" });
  const [importText, setImportText] = useState("");

  // Ссылки для обработчика WebSocket: подписка одна на всё время жизни шага.
  const jobRef = useRef<DiscoveryJob | null>(null);
  const camerasRef = useRef<CameraOut[]>([]);
  jobRef.current = job;
  camerasRef.current = cameras;

  const reload = useCallback(() => {
    api<CameraOut[]>("/api/v1/cameras").then(setCameras).catch(() => undefined);
  }, []);

  useEffect(() => {
    reload();
    return connectRealtime((m) => {
      const current = jobRef.current;
      if (m.type === "discovery_progress" && current && m["job_id"] === current.id) {
        setJob((j) => (j ? { ...j, hosts_scanned: Number(m["hosts_scanned"]) } : j));
      }
      if (m.type === "discovery_done") {
        const done = m["job"] as DiscoveryJob;
        if (current && done.id !== current.id) return;
        setJob(done);
        const existing = new Set(camerasRef.current.map((c) => hostOf(c.camera.main_url)));
        // По умолчанию отмечены все найденные камеры, которые ещё не подключены.
        setSelected(new Set(done.devices.filter((d) => d.candidates.length && !existing.has(d.host)).map((d) => d.host)));
      }
    });
  }, [reload]);

  const discover = async () => {
    setError(null);
    setResults([]);
    try {
      setJob(await post<DiscoveryJob>("/api/v1/cameras/discover", { credentials: creds.filter((c) => c.username) }));
    } catch (e) {
      setError(errText(e));
    }
  };

  const addSelected = async () => {
    if (!job) return;
    setBusy(true);
    try {
      setResults(await post<AddResult[]>(`/api/v1/cameras/discover/${job.id}/add`, { hosts: [...selected] }));
      reload();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const addManual = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await post("/api/v1/cameras", {
        name: manual.name,
        main_url: manual.main_url,
        sub_url: manual.sub_url || null,
        credentials: manual.username ? { username: manual.username, password: manual.password } : null,
      });
      setManual({ name: "", main_url: "", sub_url: "", username: "", password: "" });
      reload();
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  };

  const doImport = async () => {
    setBusy(true);
    try {
      const fmt = importText.trimStart().startsWith("-") || importText.includes("cameras:") ? "yaml" : "csv";
      setResults(await post<AddResult[]>("/api/v1/cameras/import", { format: fmt, content: importText }));
      reload();
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  };

  const existingHosts = new Set(cameras.map((c) => hostOf(c.camera.main_url)));

  return (
    <div>
      <div className="card">
        <p className="muted">{t("cams.credsHint")}</p>
        {creds.map((c, i) => (
          <div className="row" key={i}>
            <input placeholder={t("login.username")} value={c.username}
              onChange={(e) => setCreds(creds.map((x, j) => (j === i ? { ...x, username: e.target.value } : x)))} />
            <input type="password" placeholder={t("login.password")} value={c.password}
              onChange={(e) => setCreds(creds.map((x, j) => (j === i ? { ...x, password: e.target.value } : x)))} />
          </div>
        ))}
        <div className="row">
          <button type="button" className="link" onClick={() => setCreds([...creds, { username: "", password: "" }])}>
            {t("cams.addPair")}
          </button>
          <button type="button" onClick={() => void discover()} disabled={job?.status === "running"}>
            {job?.status === "running" ? t("cams.discovering") : t("cams.discover")}
          </button>
        </div>
        {job && (
          <p className="muted">
            {job.subnets.join(", ")} · {t("cams.scanned")}: {job.hosts_scanned}/{job.hosts_total}
            {job.status === "done" && ` · ${t("cams.found")}: ${job.devices.length}`}
          </p>
        )}
        {job?.warnings.map((w) => <p key={w} className="muted">⚠ {w}</p>)}
        {job?.error && <p className="alert">{job.error}</p>}
        {job?.status === "done" && job.devices.length > 0 && (
          <>
            <table className="table">
              <tbody>
                {job.devices.map((d) => {
                  const already = existingHosts.has(d.host);
                  return (
                    <tr key={d.host}>
                      <td>
                        <input type="checkbox" disabled={already || !d.candidates.length} checked={selected.has(d.host)}
                          onChange={(e) => {
                            const s = new Set(selected);
                            if (e.target.checked) s.add(d.host);
                            else s.delete(d.host);
                            setSelected(s);
                          }} />
                      </td>
                      <td>{d.host}</td>
                      <td>{d.name ?? [d.manufacturer, d.model].filter(Boolean).join(" ")}</td>
                      <td className="muted">
                        {d.methods.join(", ")}
                        {d.channels > 1 && ` · каналов: ${d.channels}`}
                      </td>
                      <td>
                        {already ? <span className="muted">{t("cams.already")}</span>
                          : d.auth === "failed" ? <span className="bad">{t("cams.authFailed")}</span> : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <button type="button" onClick={() => void addSelected()} disabled={busy || selected.size === 0}>
              {busy ? t("cams.adding") : `${t("cams.addSelected")} (${selected.size})`}
            </button>
          </>
        )}
        {results.length > 0 && (
          <ul>
            {results.map((r, i) => (
              <li key={i} className={r.ok ? "ok" : "bad"}>
                {r.ok ? `✓ ${r.name} (${r.camera_id})` : `✗ ${r.host}: ${r.error}`}
                {r.warnings.map((w) => <span key={w} className="muted"> — {w}</span>)}
              </li>
            ))}
          </ul>
        )}
        {error && <p className="alert">{error}</p>}
      </div>

      <details className="card">
        <summary>{t("cams.manual")}</summary>
        <form onSubmit={(e) => void addManual(e)}>
          <input placeholder="Имя" value={manual.name} onChange={(e) => setManual({ ...manual, name: e.target.value })} required />
          <input placeholder={t("cams.mainUrl")} value={manual.main_url} onChange={(e) => setManual({ ...manual, main_url: e.target.value })} required />
          <input placeholder={t("cams.subUrl")} value={manual.sub_url} onChange={(e) => setManual({ ...manual, sub_url: e.target.value })} />
          <div className="row">
            <input placeholder={t("login.username")} value={manual.username} onChange={(e) => setManual({ ...manual, username: e.target.value })} />
            <input type="password" placeholder={t("login.password")} value={manual.password} onChange={(e) => setManual({ ...manual, password: e.target.value })} />
          </div>
          <button type="submit" disabled={busy}>{t("cams.add")}</button>
        </form>
      </details>

      <details className="card">
        <summary>{t("cams.import")}</summary>
        <p className="muted">{t("cams.importHint")}</p>
        <textarea rows={5} value={importText} onChange={(e) => setImportText(e.target.value)} />
        <button type="button" onClick={() => void doImport()} disabled={busy || !importText.trim()}>{t("cams.add")}</button>
      </details>

      <h2>{t("cams.connected")}</h2>
      <table className="table">
        <tbody>
          {cameras.map((c) => (
            <tr key={c.camera.id}>
              <td>{c.camera.id}</td>
              <td>{c.camera.name}</td>
              <td className="muted">{c.probes.map((p) => `${p.role} ${p.width}×${p.height} ${p.codec} ${p.fps}fps`).join(" · ")}</td>
              <td>{t(`cstatus.${c.status}` as MessageKey)}</td>
              <td>
                <button type="button" className="link" onClick={() => void patch(`/api/v1/cameras/${c.camera.id}`, { enabled: !c.camera.enabled }).then(reload)}>
                  {c.camera.enabled ? t("cams.disable") : t("cams.enable")}
                </button>{" "}
                <button type="button" className="link" onClick={() => void post(`/api/v1/cameras/${c.camera.id}/baseline`).then(reload)}>
                  {t("cams.baseline")}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function hostOf(url: string): string {
  return url.replace(/^[a-z]+:\/\//, "").split(/[/:]/)[0] ?? url;
}

// ---------------------------------------------------------------- шаг 7

interface AlertsConfig {
  channels: {
    telegram: { enabled: boolean; bot_token_secret: string; timeout_s: number };
    local_relay: { enabled: boolean; http: { url: string | null; method: string; timeout_s: number } };
    [k: string]: unknown;
  };
  groups: Record<string, { roles: string[]; zones: string[]; shifts: string[]; telegram_chat_ids: string[] }>;
  telegram_token_set: boolean;
}

function AlertsStep({ t }: { t: T }) {
  const [cfg, setCfg] = useState<AlertsConfig | null>(null);
  const [token, setToken] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [tests, setTests] = useState<Delivery[]>([]);

  const load = () => api<AlertsConfig>("/api/v1/setup/alerts").then(setCfg).catch((e: unknown) => setMsg(errText(e)));
  useEffect(() => void load(), []);
  if (!cfg) return <p>{msg ?? t("loading")}</p>;

  const chats = (group: string) => (cfg.groups[group]?.telegram_chat_ids ?? []).join(", ");
  const setChats = (group: string, value: string) => {
    const g = cfg.groups[group];
    if (!g) return;
    setCfg({ ...cfg, groups: { ...cfg.groups, [group]: { ...g, telegram_chat_ids: value.split(",").map((s) => s.trim()).filter(Boolean) } } });
  };

  const save = async (e: FormEvent) => {
    e.preventDefault();
    try {
      if (token) await put("/api/v1/setup/alerts/telegram-token", { token });
      const { telegram_token_set: _ignored, ...rest } = cfg;
      void _ignored;
      await put("/api/v1/setup/alerts", { channels: rest.channels, groups: rest.groups });
      setToken("");
      setMsg(t("saved"));
      void load();
    } catch (err) {
      setMsg(errText(err));
    }
  };

  const test = async (channel: string) => {
    try {
      setTests(await post<Delivery[]>("/api/v1/setup/alerts/test", { channel, stream: "system" }));
    } catch (err) {
      setMsg(errText(err));
    }
  };

  const relay = cfg.channels.local_relay;
  return (
    <form className="card" onSubmit={(e) => void save(e)}>
      <fieldset>
        <legend>{t("alerts.telegram")}</legend>
        <label className="inline">
          <input type="checkbox" checked={cfg.channels.telegram.enabled}
            onChange={(e) => setCfg({ ...cfg, channels: { ...cfg.channels, telegram: { ...cfg.channels.telegram, enabled: e.target.checked } } })} />
          {t("alerts.enabled")}
        </label>
        <label>
          {t("alerts.token")} {cfg.telegram_token_set && <span className="muted">({t("alerts.tokenSet")})</span>}
          <input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" />
        </label>
        <label>
          {t("alerts.chats")}
          <input value={chats("engineers")} onChange={(e) => setChats("engineers", e.target.value)} />
        </label>
        <label>
          {t("alerts.opChats")}
          <input value={chats("shift_operators")} onChange={(e) => setChats("shift_operators", e.target.value)} />
        </label>
        <button type="button" onClick={() => void test("telegram")}>{t("alerts.test")}</button>
      </fieldset>
      <fieldset>
        <legend>{t("alerts.relay")}</legend>
        <label className="inline">
          <input type="checkbox" checked={relay.enabled}
            onChange={(e) => setCfg({ ...cfg, channels: { ...cfg.channels, local_relay: { ...relay, enabled: e.target.checked } } })} />
          {t("alerts.enabled")}
        </label>
        <input value={relay.http.url ?? ""} placeholder="http://10.0.5.50/siren"
          onChange={(e) => setCfg({ ...cfg, channels: { ...cfg.channels, local_relay: { ...relay, http: { ...relay.http, url: e.target.value || null } } } })} />
        <button type="button" onClick={() => void test("local_relay")}>{t("alerts.test")}</button>
      </fieldset>
      <button type="button" onClick={() => void test("web_ui")}>{t("alerts.testSystem")} (web)</button>
      {tests.length > 0 && (
        <ul>
          {tests.map((r, i) => (
            <li key={i} className={r.ok ? "ok" : "bad"}>{r.channel} → {r.target}: {r.ok ? "OK" : r.error}</li>
          ))}
        </ul>
      )}
      {msg && <p className="muted">{msg}</p>}
      <button type="submit">{t("save")}</button>
    </form>
  );
}

// ---------------------------------------------------------------- страница

export function SetupPage({ t }: { t: T }) {
  const [step, setStep] = useState<Step>("cameras");
  return (
    <section>
      <h1>{t("setup.title")}</h1>
      <div className="tabs" role="tablist">
        {STEPS.map((s) => (
          <button key={s.id} type="button" role="tab" aria-selected={step === s.id} onClick={() => setStep(s.id)}>
            {t(s.title)}
          </button>
        ))}
      </div>
      <p className="muted">{t("setup.later")}</p>
      {step === "site" && <SiteStep t={t} />}
      {step === "hardware" && <HardwareStep t={t} />}
      {step === "cameras" && <CamerasStep t={t} />}
      {step === "zones" && <ZonesStep t={t} />}
      {step === "alerts" && <AlertsStep t={t} />}
      {step === "selftest" && <SelfTestStep t={t} />}
    </section>
  );
}
