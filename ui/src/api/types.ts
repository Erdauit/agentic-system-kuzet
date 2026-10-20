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
  camera: { id: string; name: string; main_url: string; sub_url: string | null; enabled: boolean; vendor: string | null; model: string | null; location: string | null; scenarios: string[] };
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

// ---------------------------------------------------------------- Фаза 2: инциденты, зоны, самотест

export type EventType = "fall" | "red_zone" | "smoke_fire" | "fall_recovered";
export type IncidentState = "OPEN" | "ACKNOWLEDGED" | "RESOLVED" | "FALSE_ALARM";
export type ReasonCode = "steam" | "welding" | "repair_lying" | "reflection" | "other";

export interface FrameRef {
  blob_key: string;
  captured_at: string;
  bbox: { x1: number; y1: number; x2: number; y2: number } | null;
}

export interface ClipRef {
  blob_key: string;
  starts_at: string;
  ends_at: string;
  codec: string;
}

export interface QEvent {
  id: string;
  type: EventType;
  camera_id: string;
  zone_id: string | null;
  track_ids: number[];
  started_at: string;
  detected_at: string;
  detector_score: number;
  routing: "event" | "event_with_arbiter";
  model_version: string;
  evidence: FrameRef[];
  area: string | null;
  synthetic: "selftest" | "canary" | null;
}

export interface Incident {
  id: string;
  type: Exclude<EventType, "fall_recovered">;
  state: IncidentState;
  severity: "critical" | "high" | "medium" | "low";
  zone_id: string | null;
  camera_ids: string[];
  event_ids: string[];
  opened_at: string;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  resolved_at: string | null;
  clip: ClipRef | null;
  keyframes: FrameRef[];
  area: string | null;
  synthetic: "selftest" | "canary" | null;
  escalation_level: number;
}

export interface IncidentDelivery {
  channel: string;
  target: string;
  level: number;
  status: "pending" | "sent" | "retrying" | "failed";
  attempt: number;
  error: string | null;
  sent_at: string | null;
}

export interface IncidentOut {
  incident: Incident;
  events: QEvent[];
  deliveries: IncidentDelivery[];
}

export interface Point {
  x: number;
  y: number;
}

export interface Zone {
  id: string;
  camera_id: string;
  name: string;
  kind: "red_zone" | "monitoring";
  polygon: { points: Point[] };
  dwell_seconds: number | null;
  work_schedule: unknown[];
  area: string | null;
}

export interface CalibrationOut {
  calibration: {
    image_points: Point[];
    known_distance: { point_a: number; point_b: number; meters: number } | null;
    homography: number[][] | null;
    calibrated_at: string;
  } | null;
  metric: boolean;
}

export interface SelfTestStatus {
  id: string;
  started_at: string;
  slo_seconds: number;
  cameras: { camera_id: string; event_id: string; event_received: boolean; evidence: boolean; incident_id: string | null }[];
  incidents: {
    incident_id: string;
    camera_ids: string[];
    area: string | null;
    clip: ClipRef | null;
    keyframes: number;
    first_alert_seconds: number | null;
    within_slo: boolean;
    deliveries: { channel: string; target: string; status: string; attempt: number; error: string | null }[];
  }[];
  all_delivered: boolean;
  clip_ready: boolean;
  confirmed_at: string | null;
  confirmed_by: string | null;
}

export const mediaUrl = (key: string) => `/api/v1/media/${key.split("/").map(encodeURIComponent).join("/")}`;
