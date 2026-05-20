export interface Tenant {
  tenant_id: string;
  name: string;
  email: string;
  phone: string;
  status: string;
  notes: string;
  created_at: string;
  updated_at: string;
}

export interface BrokerAccount {
  broker_account_id: string;
  tenant_id: string;
  broker_type: string;
  display_name: string;
  client_code: string;
  trading_mode: string;
  enabled: boolean;
  default_strategies: string[];
  created_at: string;
  updated_at: string;
}

export interface Position {
  symbol: string;
  quantity: number;
  net_qty: number;
  qty_lots: number;
  lot_size: number;
  avg_price: number;
  entry_price: number;
  entry_ts: string;
  product_type: string;
  side?: string;
  ltp?: number;
  unrealized_pnl?: number;
}

export interface Order {
  order_id: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  order_type: string;
  status: string;
  quantity: number;
  filled_qty?: number;
  filled_quantity?: number;
  price: number;
  avg_price: number;
  product_type: string;
  created_at: string;
  updated_at?: string;
  correlation_id?: string;
  request_id?: string;
  events?: OrderEvent[];
}

export interface OrderEvent {
  status: string;
  timestamp: string;
  message?: string;
}

export interface Trade {
  trade_id?: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  entry_price?: number;
  exit_price?: number;
  price?: number;
  quantity?: number;
  qty?: number;
  pnl?: number;
  entry_time?: string;
  exit_time?: string;
  trade_time?: string;
  timestamp?: string;
  strategy_id?: string;
  broker_account_id?: string;
  order_id?: string;
}

export interface PnLSnapshot {
  realized_pnl: number;
  unrealized_pnl: number;
  gross_exposure: number;
  invalid_marks_count?: number;
  as_of?: string | null;
  session_date?: string | null;
}

export interface BalanceResponse {
  tenant_id: string;
  broker_account_id: string;
  balance: {
    net_worth: number;
    available_for_trade: number;
    utilized: number;
    margin_used: number;
  };
}

export interface AuditEvent {
  timestamp: string;
  actor: string;
  action: string;
  resource_type: string;
  resource_id: string;
  before?: Record<string, unknown>;
  after?: Record<string, unknown>;
  request_id?: string;
}

export interface Strategy {
  strategy_id?: string;
  name?: string;
  enabled: boolean;
  last_updated?: string;
}
