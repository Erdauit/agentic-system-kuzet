import { useEffect, useState } from "react";

import { api, onUnauthorized, post } from "./api/client";
import { isAdmin, type User } from "./api/types";
import { initialLang, saveLang, translate, type Lang, type MessageKey } from "./i18n";
import { HealthPage } from "./pages/Health/HealthPage";
import { ChangePasswordPage, LoginPage } from "./pages/Login/LoginPage";
import { OperationsPage } from "./pages/Operations/OperationsPage";
import { SetupPage } from "./pages/Setup/SetupPage";

interface Section {
  id: string;
  title: MessageKey;
  // Фаза SPEC.md §12, в которой раздел появится. null — раздел уже работает.
  phase: number | null;
  adminOnly?: boolean;
}

const SECTIONS: Section[] = [
  { id: "setup", title: "nav.setup", phase: null, adminOnly: true },
  { id: "operations", title: "nav.operations", phase: null },
  { id: "health", title: "nav.health", phase: null },
  { id: "chat", title: "nav.chat", phase: 5 },
  { id: "models", title: "nav.models", phase: 7 },
  { id: "tasks", title: "nav.tasks", phase: 7 },
  { id: "labeling", title: "nav.labeling", phase: 6 },
  { id: "settings", title: "nav.settings", phase: 9 },
];

function currentSection(): string {
  const id = window.location.hash.replace(/^#\/?/, "");
  return SECTIONS.some((s) => s.id === id && s.phase === null) ? id : "operations";
}

export function App() {
  const [lang, setLang] = useState<Lang>(initialLang);
  const [section, setSection] = useState<string>(currentSection);
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const t = (key: MessageKey) => translate(lang, key);

  useEffect(() => {
    api<User>("/api/v1/auth/me").then(setUser).catch(() => setUser(null));
    return onUnauthorized(() => setUser(null));
  }, []);

  useEffect(() => {
    const onHash = () => setSection(currentSection());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    document.documentElement.lang = lang;
    saveLang(lang);
  }, [lang]);

  const logout = async () => {
    await post("/api/v1/auth/logout").catch(() => undefined);
    setUser(null);
  };

  const visible = SECTIONS.filter((s) => !s.adminOnly || isAdmin(user ?? null));

  let content;
  if (user === undefined) content = <p>{t("loading")}</p>;
  else if (user === null) content = <LoginPage t={t} onLogin={setUser} />;
  else if (user.must_change_password) content = <ChangePasswordPage t={t} onDone={setUser} />;
  else if (section === "setup" && isAdmin(user)) content = <SetupPage t={t} />;
  else if (section === "operations") content = <OperationsPage t={t} />;
  else content = <HealthPage lang={lang} t={t} user={user} />;

  return (
    <div className="layout">
      <header className="topbar">
        <span className="brand">{t("appTitle")}</span>
        <div className="row">
          {user && (
            <>
              <span className="muted">
                {user.display_name} ({user.role})
              </span>
              <button type="button" className="link" onClick={() => void logout()}>
                {t("logout")}
              </button>
            </>
          )}
          <div className="lang-switch" role="group" aria-label="Язык / Тіл">
            {(["ru", "kk"] as const).map((l) => (
              <button key={l} type="button" aria-pressed={lang === l} onClick={() => setLang(l)}>
                {t(`lang.${l}`)}
              </button>
            ))}
          </div>
        </div>
      </header>
      <nav className="sidebar">
        <ul>
          {visible.map((s) =>
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
      <main className="content">{content}</main>
    </div>
  );
}
