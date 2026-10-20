import { useState, type FormEvent } from "react";

import { ApiError, post } from "../../api/client";
import type { User } from "../../api/types";
import type { T } from "../../i18n";

export function LoginPage({ t, onLogin }: { t: T; onLogin: (u: User) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onLogin(await post<User>("/api/v1/auth/login", { username, password }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="card narrow" onSubmit={(e) => void submit(e)}>
      <h1>{t("login.title")}</h1>
      <label>
        {t("login.username")}
        <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
      </label>
      <label>
        {t("login.password")}
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
      </label>
      {error && <p className="alert">{error}</p>}
      <button type="submit" disabled={busy}>
        {t("login.submit")}
      </button>
    </form>
  );
}

export function ChangePasswordPage({ t, onDone }: { t: T; onDone: (u: User) => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (next !== repeat) return setError(t("login.mismatch"));
    try {
      onDone(await post<User>("/api/v1/auth/change-password", { current_password: current, new_password: next }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  };

  return (
    <form className="card narrow" onSubmit={(e) => void submit(e)}>
      <h1>{t("login.change")}</h1>
      <label>
        {t("login.current")}
        <input type="password" value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" required />
      </label>
      <label>
        {t("login.new")}
        <input type="password" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" minLength={10} required />
      </label>
      <label>
        {t("login.repeat")}
        <input type="password" value={repeat} onChange={(e) => setRepeat(e.target.value)} autoComplete="new-password" required />
      </label>
      {error && <p className="alert">{error}</p>}
      <button type="submit">{t("save")}</button>
    </form>
  );
}
