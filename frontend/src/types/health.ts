export interface AccountStaleness {
  broker_account_id: string;
  last_sync_ts: string | null;
  error_reason: string | null;
  stale: boolean;
}

export interface HealthSummary {
  status: 'ok' | 'degraded' | 'unknown';
  ready?: boolean;
  service: string;
  timestamp: string;
  schema_status?: string;
  operating_mode: string | null;
  trade_mode?: string | null;
  stream_worker_running?: boolean;
  stream_worker_expected?: boolean;
  watchdog_running?: boolean;
  last_position_sync_ok_ts: string | null;
  last_broker_blocked_or_rate_limited_ts: string | null;
  tracked_account_count?: number;
  degraded_reasons?: string[];
  readiness?: {
    ready: boolean;
    http_status: number;
    reason?: string | null;
    degraded_scope_count?: number;
    position_state_counts?: Record<string, number>;
    degraded_positions?: number;
    reconciling_positions?: number;
    blocking_count?: number;
  };
  schema?: { status: string; checked_at: string; missing_tables: string[]; missing_indexes: string[] };
  oi_ml_shadow_ingestion?: {
    enabled: boolean;
    status: 'ok' | 'degraded' | 'unknown' | 'disabled';
    reason?: string | null;
    underlying?: string;
    provider?: string;
    dry_run_only?: boolean;
    live_order_path_enabled?: boolean;
    checked_at?: string;
    snapshot_expected?: boolean;
    option_chain?: {
      today_row_count: number;
      latest_snapshot_ts?: string | null;
      latest_source_ts?: string | null;
      latest_ingested_at?: string | null;
      latest_ingested_age_seconds?: number | null;
    };
    validation_reports?: {
      today_report_count: number;
      latest_validation_ts?: string | null;
      latest_status?: string;
      latest_severity?: string;
      latest_primary_quote_count?: number;
      latest_reference_quote_count?: number;
    };
    shadow_intents?: {
      today_intent_count: number;
      latest_created_at?: string | null;
    };
  };
  watchdog: Record<string, unknown>;
  per_account_staleness: AccountStaleness[];
  alerts?: { firing_count: number; firing_rules: string[] };
  auto_mitigation: { enabled: boolean; total_events: number };
  kill_switch?: {
    ready?: boolean;
    reason?: string | null;
    degraded_reason?: string | null;
    active_count?: number;
    source?: string;
    divergent?: boolean;
    legacy_active?: boolean;
  };
  leader_lease?: Record<string, unknown>;
  position_record_invariants?: {
    terminal_nonzero_net_qty_count?: number;
    error?: string | null;
  };
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
