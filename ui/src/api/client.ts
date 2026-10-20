// HTTP-клиент UI: cookie-сессия, CSRF-заголовок на изменяющих запросах, понятные ошибки.

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

type Listener = () => void;
const unauthorizedListeners = new Set<Listener>();

export function onUnauthorized(fn: Listener): () => void {
  unauthorizedListeners.add(fn);
  return () => unauthorizedListeners.delete(fn);
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (method !== "GET") headers.set("x-qoima-request", "1");
  if (init.body !== undefined && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const resp = await fetch(path, { ...init, method, headers, credentials: "same-origin" });
  if (resp.status === 401) unauthorizedListeners.forEach((fn) => fn());
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = (await resp.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail)) detail = body.detail.map((d: { msg?: string }) => d.msg ?? "").join("; ");
    } catch {
      // тело не JSON — оставляем код ответа
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? null : JSON.stringify(body) });
export const put = <T>(path: string, body: unknown) => api<T>(path, { method: "PUT", body: JSON.stringify(body) });
export const patch = <T>(path: string, body: unknown) => api<T>(path, { method: "PATCH", body: JSON.stringify(body) });
