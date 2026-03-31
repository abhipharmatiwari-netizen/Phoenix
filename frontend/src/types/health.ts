export interface AccountStaleness {
  broker_account_id: string;
  last_sync_ts: string | null;
  error_reason: string | null;
  stale: boolean;
}

export interface HealthSummary {
  status: 'ok' | 'degraded';
  service: string;
  timestamp: string;
  schema_status: string;
  operating_mode: string | null;
  stream_worker_running: boolean;
  stream_worker_expected: boolean;
  watchdog_running: boolean;
  last_position_sync_ok_ts: string | null;
  last_broker_blocked_or_rate_limited_ts: string | null;
  tracked_account_count: number;
  degraded_reasons: string[];
  schema: { status: string; checked_at: string; missing_tables: string[]; missing_indexes: string[] };
  watchdog: Record<string, unknown>;
  per_account_staleness: AccountStaleness[];
  alerts: { firing_count: number; firing_rules: string[] };
  auto_mitigation: { enabled: boolean; total_events: number };
}

export interface AlertRule {
  rule_name: string;
  state: 'ok' | 'firing' | 'resolved';
  severity: 'info' | 'warning' | 'critical';
  value?: number | string | null;
  message?: string;
  fired_at?: number | null;
  resolved_at?: number | null;
  labels?: Record<string, string>;
}

export interface AlertsResponse {
  total_rules: number;
  firing_count: number;
  alerts: AlertRule[];
}

export interface MitigationEvent {
  rule_name: string;
  action: string;
  scope_key: string;
  scope_value: string;
  fault_count: number;
  timestamp: number;
  details?: Record<string, unknown>;
}

export interface MitigationsResponse {
  enabled: boolean;
  rule_count: number;
  total_events: number;
  recent_events: MitigationEvent[];
  fault_counts: Record<string, Record<string, number>>;
}
