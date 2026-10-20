// Типы ответов API. Повторяют контракты shared.schemas (снапшоты — tests/contract/snapshots/).

export type Role = "operator" | "shift_lead" | "site_admin" | "qoima_engineer";
export type HealthStatus = "green" | "yellow" | "red" | "unknown" | "not_deployed";
export type CameraStatus = "ok" | "degraded" | "down" | "unknown";

export interface User {
  id: string;
  username: string;
  display_name: string;
  role: Role;
  must_change_password: boolean;
}

export interface ComponentHealth {
  component: string;
  kind: string;
  status: HealthStatus;
  message: string;
  checked_at: string;
  latency_ms: number | null;
}

export interface SystemHealth {
  status: HealthStatus;
  components: ComponentHealth[];
  generated_at: string;
}

export interface CameraHealth {
  camera_id: string;
  checked_at: string;
  status: CameraStatus;
  stream_alive: boolean;
  fps_in: number;
  fps_planned: number;
  reconnects_total: number;
  decode_errors_total: number;
  brightness: number | null;
  sharpness_ratio: number | null;
  occluded_ratio: number | null;
  view_shift_score: number | null;
  issues: string[];
}

export interface StreamProbe {
  role: "main" | "sub";
  url: string;
  codec: string;
  width: number;
  height: number;
  fps: number;
  bitrate_kbps: number | null;
}

export interface CameraOut {
  camera: { id: string; name: string; main_url: string; sub_url: string | null; enabled: boolean; vendor: string | null; model: string | null; location: string | null };
  status: CameraStatus;
  health: CameraHealth | null;
  probes: StreamProbe[];
  has_credentials: boolean;
  live_available: boolean;
}

export interface StreamCandidate {
  url: string;
  source: string;
  profile: string | null;
  width: number | null;
  height: number | null;
  channel: number;
}

export interface DiscoveredDevice {
  host: string;
  methods: string[];
  manufacturer: string | null;
  model: string | null;
  name: string | null;
  auth: "ok" | "failed" | "not_required" | "unknown";
  channels: number;
  candidates: StreamCandidate[];
  errors: string[];
}

export interface DiscoveryJob {
  id: string;
  status: "running" | "done" | "failed";
  subnets: string[];
  hosts_total: number;
  hosts_scanned: number;
  devices: DiscoveredDevice[];
  warnings: string[];
  error: string | null;
}

export interface AddResult {
  host: string;
  ok: boolean;
  camera_id: string | null;
  name: string | null;
  error: string | null;
  warnings: string[];
}

export interface SystemAlert {
  id: string;
  kind: string;
  severity: "warning" | "critical";
  component: string;
  camera_id: string | null;
  message: string;
  raised_at: string;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  cleared_at: string | null;
}

export interface SloRow {
  name: string;
  title: string;
  target_s: number;
  value_s: number | null;
  status: "ok" | "violated" | "not_measured";
  note: string | null;
}

export interface DoctorOutcome {
  name: string;
  level: "ok" | "warn" | "fail" | "skip";
  detail: string;
  recommendation: string;
}

export interface HardwareOut {
  report: { generated_at: string; hostname: string; outcomes: DoctorOutcome[] } | null;
  verdict?: string;
  age_minutes?: number;
  stale?: boolean;
  message?: string;
}

export interface Shift {
  name: string;
  start: string;
  end: string;
}

export interface SiteSettings {
  name: string;
  ui_languages: ("ru" | "kk")[];
  default_language: "ru" | "kk";
  timezone: string;
  shifts: Shift[];
  discovery_subnets: string[];
}

export interface Delivery {
  channel: string;
  target: string;
  ok: boolean;
  error: string | null;
}

export const isAdmin = (u: User | null) => u !== null && (u.role === "site_admin" || u.role === "qoima_engineer");
