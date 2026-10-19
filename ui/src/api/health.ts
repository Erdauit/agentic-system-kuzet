// Типы повторяют контракт shared.schemas.health (SystemHealth / ComponentHealth).
// Снапшот JSON Schema — tests/contract/snapshots/SystemHealth.schema.json.

export type HealthStatus = "green" | "yellow" | "red" | "unknown" | "not_deployed";

export type DetailValue = string | number | boolean | null;

export interface ComponentHealth {
  component: string;
  kind: string;
  status: HealthStatus;
  message: string;
  checked_at: string;
  latency_ms: number | null;
  details: Record<string, DetailValue>;
}

export interface SystemHealth {
  status: HealthStatus;
  components: ComponentHealth[];
  generated_at: string;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function fetchSystemHealth(signal?: AbortSignal): Promise<SystemHealth> {
  const resp = await fetch("/api/v1/health", { signal: signal ?? null, headers: { Accept: "application/json" } });
  if (!resp.ok) {
    throw new ApiError(resp.status, `HTTP ${resp.status}`);
  }
  return (await resp.json()) as SystemHealth;
}
