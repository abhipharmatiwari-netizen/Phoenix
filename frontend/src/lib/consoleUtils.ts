import { HealthSummary } from '../types/health';

export type ConsoleStatus = 'healthy' | 'degraded' | 'blocked' | 'unknown';

export const HEALTH_STALE_MS = 45_000;

const SECRET_KEY_PATTERN = /(secret|password|passwd|token|authorization|cookie|credential|api[_-]?key|private[_-]?key|pin|totp|jwt|session)/i;

export function statusLabel(status: ConsoleStatus): string {
  switch (status) {
    case 'healthy':
      return 'Healthy';
    case 'degraded':
      return 'Degraded';
    case 'blocked':
      return 'Blocked';
    default:
      return 'Unknown';
  }
}

export function statusClass(status: ConsoleStatus): string {
  return `status-chip status-chip--${status}`;
}

export function normalizeStatus(value: unknown): ConsoleStatus {
  const token = String(value || '').trim().toLowerCase();
  if (['ok', 'ready', 'running', 'active', 'healthy', 'enabled', 'synced'].includes(token)) {
    return 'healthy';
  }
  if (['degraded', 'warning', 'stale', 'pending', 'partial'].includes(token)) {
    return 'degraded';
  }
  if (['blocked', 'error', 'failed', 'firing', 'tripped', 'critical', 'not_ready', 'not ready'].includes(token)) {
    return 'blocked';
  }
  return 'unknown';
}

export function coerceRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

export function displayValue(value: unknown, fallback = 'Unknown'): string {
  if (value === null || value === undefined || value === '') {
    return fallback;
  }
  if (typeof value === 'boolean') {
    return value ? 'Yes' : 'No';
  }
  if (Array.isArray(value)) {
    return value.length ? value.map((item) => displayValue(item, '')).filter(Boolean).join(', ') : fallback;
  }
  if (typeof value === 'object') {
    return JSON.stringify(redactSensitive(value));
  }
  return String(value);
}

export function formatDateTime(value: unknown): string {
  if (!value) {
    return 'Unknown';
  }
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) {
    return 'Unknown';
  }
  return date.toLocaleString();
}

export function formatAge(value: unknown, now = Date.now()): string {
  if (!value) {
    return 'Unknown';
  }
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) {
    return 'Unknown';
  }
  const seconds = Math.max(0, Math.round((now - date.getTime()) / 1000));
  if (seconds < 60) {
    return `${seconds}s`;
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 120) {
    return `${minutes}m`;
  }
  return `${Math.round(minutes / 60)}h`;
}

export function isFreshTimestamp(value: unknown, maxAgeMs = HEALTH_STALE_MS, now = Date.now()): boolean {
  if (!value) {
    return false;
  }
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) {
    return false;
  }
  return now - date.getTime() <= maxAgeMs;
}

export function healthReasons(summary: HealthSummary | null | undefined, extra?: string): string[] {
  const reasons = new Set<string>();
  if (extra) {
    reasons.add(extra);
  }
  (summary?.degraded_reasons || []).forEach((reason) => {
    if (reason) {
      reasons.add(String(reason));
    }
  });
  if (summary?.readiness?.reason) {
    reasons.add(String(summary.readiness.reason));
  }
  if (summary?.readiness?.blocking_count) {
    reasons.add(`position_authority_blocking=${summary.readiness.blocking_count}`);
  }
  return Array.from(reasons);
}

export function classifyOperatorHealth(
  summary: HealthSummary | null,
  source: 'admin' | 'public' | 'unavailable',
  now = Date.now(),
): ConsoleStatus {
  if (!summary || source === 'unavailable') {
    return 'unknown';
  }
  if (source !== 'admin') {
    return 'unknown';
  }
  if (!isFreshTimestamp(summary.timestamp, HEALTH_STALE_MS, now)) {
    return 'unknown';
  }
  const readinessReady = summary.readiness?.ready;
  if (readinessReady === false || summary.status === 'degraded') {
    return 'blocked';
  }
  if (summary.status === 'ok' && readinessReady === true) {
    return 'healthy';
  }
  return 'unknown';
}

export function redactSensitive(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => redactSensitive(item));
  }
  if (!value || typeof value !== 'object') {
    return value;
  }
  const out: Record<string, unknown> = {};
  Object.entries(value as Record<string, unknown>).forEach(([key, item]) => {
    out[key] = SECRET_KEY_PATTERN.test(key) ? '[REDACTED]' : redactSensitive(item);
  });
  return out;
}

export function compactJson(value: unknown): string {
  return JSON.stringify(redactSensitive(value), null, 2);
}
