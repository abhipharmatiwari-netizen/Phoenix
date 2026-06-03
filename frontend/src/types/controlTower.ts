export interface MatrixTenant {
  tenant_id: string;
  name: string;
}

export interface MatrixStrategy {
  strategy_id: string;
  display_name: string;
}

export interface ControlTowerCapability {
  read_only: boolean;
  mutation_enabled: boolean;
  routes_disabled: boolean;
  trade_mode: string;
  reason_required: boolean;
  management_disabled_reason: string | null;
  blocking_reasons: string[];
}

export interface ControlTowerAccountStatus {
  tenant_id: string;
  broker_account_id: string;
  display_name?: string | null;
  trading_mode?: string | null;
}

export interface ControlTowerStrategyConfigStatus {
  tenant_id: string;
  broker_account_id: string;
  strategy_id: string;
  strategy_config_id: string;
  enabled: boolean;
}

export interface MatrixResponse {
  tenants: MatrixTenant[];
  strategies: MatrixStrategy[];
  matrix: Record<string, Record<string, boolean>>;
  capability?: ControlTowerCapability | null;
  active_accounts?: ControlTowerAccountStatus[];
  enabled_strategy_configs?: ControlTowerStrategyConfigStatus[];
  routed_strategy_ids?: string[];
}
