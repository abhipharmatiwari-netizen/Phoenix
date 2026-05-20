export interface Instrument {
  label: string;
  price: number;
  change_pct: number;
  tick_time: string;
  atr_series?: number[];
  rsi_series?: number[];
  macd_series?: number[];
}

export interface RecentTrade {
  label: string;
  underlying: string;
  side: 'BUY' | 'SELL';
  qty: number;
  entry_price: number;
  exit_price: number;
  pnl: number;
  template_name: string;
  strategy_name: string;
  reason: string;
  exit_ts: string;
  entry_ts: string;
}

export interface OpenPosition {
  instrument: string;
  side: 'BUY' | 'SELL';
  qty: number;
  qty_lots: number;
  lot_size: number;
  entry: number;
  last: number;
  pnl: number;
  template: string;
}

export interface HubPosition {
  tenant_id: string;
  broker_account_id: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  qty: number;
  qty_lots: number;
  lot_size: number;
  net_qty: number;
  avg_price: number;
  entry_price: number;
  entry_ts: string;
  product_type: string;
  ltp: number | null;
  unrealized_pnl?: number | null;
}

export interface PnlData {
  by_instrument: Record<string, number>;
  total: number;
  realized: number;
  open: number;
  invalid_marks_count?: number;
}

export interface StrategySelection {
  underlying: string;
  regime: string;
  selected_strategies: string[];
  reason: string;
  hold_until: string | null;
  updated_at: string;
}

export interface DashboardSnapshot {
  mode: string;
  trade_mode: string;
  live: boolean;
  timestamp: string;
  multi_hub_enabled: boolean;
  instruments: Instrument[];
  trades: { recent: RecentTrade[]; open_positions: OpenPosition[] };
  pnl: PnlData;
  strategy_selection: StrategySelection[];
  hub_positions?: HubPosition[];
}
