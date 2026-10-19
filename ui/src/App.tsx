import { useEffect, useState } from "react";

import { initialLang, saveLang, translate, type Lang, type MessageKey } from "./i18n";
import { HealthPage } from "./pages/Health/HealthPage";

interface Section {
  id: string;
  title: MessageKey;
  // Фаза SPEC.md §12, в которой раздел появится. null — раздел уже работает.
  phase: number | null;
}

const SECTIONS: Section[] = [
  { id: "setup", title: "nav.setup", phase: 1 },
  { id: "operations", title: "nav.operations", phase: 2 },
  { id: "health", title: "nav.health", phase: null },
  { id: "chat", title: "nav.chat", phase: 5 },
  { id: "models", title: "nav.models", phase: 7 },
  { id: "tasks", title: "nav.tasks", phase: 7 },
  { id: "labeling", title: "nav.labeling", phase: 6 },
  { id: "settings", title: "nav.settings", phase: 1 },
];

function currentSection(): string {
  const id = window.location.hash.replace(/^#\/?/, "");
  const section = SECTIONS.find((s) => s.id === id && s.phase === null);
  return section ? section.id : "health";
}

export function App() {
  const [lang, setLang] = useState<Lang>(initialLang);
  const [section, setSection] = useState<string>(currentSection);
  const t = (key: MessageKey) => translate(lang, key);

  useEffect(() => {
    const onHash = () => setSection(currentSection());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    document.documentElement.lang = lang;
    saveLang(lang);
  }, [lang]);

  return (
    <div className="layout">
      <header className="topbar">
        <span className="brand">{t("appTitle")}</span>
        <div className="lang-switch" role="group" aria-label="Язык / Тіл">
          {(["ru", "kk"] as const).map((l) => (
            <button key={l} type="button" aria-pressed={lang === l} onClick={() => setLang(l)}>
              {t(`lang.${l}`)}
            </button>
          ))}
        </div>
      </header>
      <nav className="sidebar">
        <ul>
          {SECTIONS.map((s) =>
            s.phase === null ? (
              <li key={s.id}>
                <a href={`#/${s.id}`} aria-current={section === s.id ? "page" : undefined}>
                  {t(s.title)}
                </a>
              </li>
            ) : (
              <li key={s.id} className="disabled" title={`${t("phase")} ${s.phase}`}>
                <span>{t(s.title)}</span>
                <span className="badge">
                  {t("phase")} {s.phase}
                </span>
              </li>
            ),
          )}
        </ul>
      </nav>
      <main className="content">{section === "health" && <HealthPage lang={lang} />}</main>
    </div>
  );
}
