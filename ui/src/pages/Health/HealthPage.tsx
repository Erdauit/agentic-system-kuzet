import { useEffect, useState } from "react";

import { fetchSystemHealth, type HealthStatus, type SystemHealth } from "../../api/health";
import { translate, type Lang, type MessageKey } from "../../i18n";

// Страница обновляется раз в 5 с: это интервал Camera Health (configs/defaults.yaml,
// camera_health.check_interval_s), чаще данные не меняются.
const REFRESH_MS = 5000;

function StatusBadge({ status, lang }: { status: HealthStatus; lang: Lang }) {
  return (
    <span className={`status status-${status}`}>{translate(lang, `status.${status}` as MessageKey)}</span>
  );
}

export function HealthPage({ lang }: { lang: Lang }) {
  const [data, setData] = useState<SystemHealth | null>(null);
  const [error, setError] = useState<boolean>(false);
  const t = (key: MessageKey) => translate(lang, key);

  useEffect(() => {
    let active = true;
    let controller = new AbortController();
    const load = async () => {
      controller.abort();
      controller = new AbortController();
      try {
        const result = await fetchSystemHealth(controller.signal);
        if (active) {
          setData(result);
          setError(false);
        }
      } catch (e) {
        if (active && !(e instanceof DOMException && e.name === "AbortError")) {
          // «Тишина ≠ норма»: недоступный core-api показываем явно, а не старыми данными.
          setError(true);
        }
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => {
      active = false;
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  return (
    <section>
      <h1>{t("health.title")}</h1>
      {error && (
        <p className="alert" role="alert">
          {t("health.unreachable")}
        </p>
      )}
      {!data && !error && <p>{t("health.loading")}</p>}
      {data && (
        <>
          <p className="overall">
            {t("health.overall")}: <StatusBadge status={error ? "unknown" : data.status} lang={lang} />
            <span className="muted">
              {" "}
              · {t("health.updated")} {new Date(data.generated_at).toLocaleTimeString(lang)}
            </span>
          </p>
          <ul className="components">
            {data.components.map((c) => (
              <li key={c.component} className={`component component-${c.status}`}>
                <div className="component-head">
                  <span className={`dot dot-${c.status}`} aria-hidden="true" />
                  <strong>{c.component}</strong>
                  <StatusBadge status={c.status} lang={lang} />
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
