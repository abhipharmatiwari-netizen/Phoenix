import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { TenantService, createDashboardWebSocketUrl, getTenantId } from '../client';
import { BrokerAccount, Position } from '../types/trading';
import { DashboardSnapshot, HubPosition } from '../types/dashboard';
import DataTable, { Column } from '../components/shared/DataTable';
import LoadingSpinner from '../components/shared/LoadingSpinner';
import StaleBanner from '../components/shared/StaleBanner';
import StatusBadge from '../components/shared/StatusBadge';
import { useWebSocket } from '../hooks/useWebSocket';

const Positions: React.FC = () => {
  const [accounts, setAccounts] = useState<BrokerAccount[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState<string>('');
  const [positions, setPositions] = useState<Position[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const websocketUrlFactory = useCallback(() => createDashboardWebSocketUrl('delta'), []);
  const { data: dashboardData, isStale: isDashboardStale } = useWebSocket(websocketUrlFactory);

  useEffect(() => {
    let active = true;
    const fetchAccounts = async () => {
      try {
        const resp = await TenantService.listMyAccounts();
        if (active) {
          setAccounts(resp.accounts);
          if (resp.accounts.length > 0) {
            setSelectedAccountId(resp.accounts[0].broker_account_id);
          }
        }
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : 'Failed to fetch accounts');
      } finally {
        if (active) setLoading(false);
      }
    };
    fetchAccounts();
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!selectedAccountId) return;
    let active = true;
    setLoading(true);
    setError(null);
    const fetchPositions = async () => {
      try {
        const resp = await TenantService.getAccountPositions(selectedAccountId);
        if (active) setPositions(resp.positions);
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : 'Failed to fetch positions');
      } finally {
        if (active) setLoading(false);
      }
    };
    fetchPositions();
    return () => { active = false; };
  }, [selectedAccountId]);

  useEffect(() => {
    if (!selectedAccountId) return;
    const snapshot = isDashboardSnapshot(dashboardData) ? dashboardData : null;
    if (!snapshot?.hub_positions) return;
    const tenantId = getTenantId();
    const livePositions = snapshot.hub_positions
      .filter((row) => row.broker_account_id === selectedAccountId && row.tenant_id === tenantId)
      .map(mapHubPositionToPosition);
    setPositions(livePositions);
    setLoading(false);
  }, [dashboardData, selectedAccountId]);

  const columns: Column<Position & Record<string, unknown>>[] = useMemo(() => [
    { key: 'tenant_id', header: 'Tenant', render: (row) => row.tenant_id || getTenantId() || 'Unknown' },
    { key: 'broker_account_id', header: 'Account', render: (row) => row.broker_account_id || selectedAccountId || 'Unknown' },
    { key: 'strategy_id', header: 'Strategy', render: (row) => row.strategy_id || 'Unknown' },
    { key: 'symbol', header: 'Contract' },
    { key: 'net_qty', header: 'Net Qty', render: (row) => String(row.net_qty ?? row.quantity ?? 0) },
    { key: 'broker_qty', header: 'Broker Qty', render: (row) => row.broker_qty ?? 'Unknown' },
    { key: 'reconciliation_state', header: 'Reconciliation', render: (row) => reconciliationBadge(row) },
    { key: 'ownership_state', header: 'Ownership', render: (row) => row.ownership_state || 'Unknown' },
    { key: 'lifecycle_state', header: 'Lifecycle', render: (row) => row.lifecycle_state || 'Unknown' },
    { key: 'mark_ts', header: 'Mark Freshness', render: (row) => row.mark_ts ? new Date(String(row.mark_ts)).toLocaleTimeString() : 'Unknown' },
    { key: 'pnl_ts', header: 'PnL Freshness', render: (row) => row.pnl_ts ? new Date(String(row.pnl_ts)).toLocaleTimeString() : 'Unknown' },
    { key: 'side', header: 'Side', render: (row) => (
      <span style={{ color: row.side === 'BUY' ? '#16a34a' : '#dc2626', fontWeight: 600 }}>
        {row.side || '-'}
      </span>
    ) },
    { key: 'quantity', header: 'Qty' },
    { key: 'avg_price', header: 'Avg Price', render: (row) => formatNum(row.avg_price) },
    { key: 'ltp', header: 'LTP', render: (row) => row.ltp != null ? formatNum(row.ltp) : '-' },
    { key: 'unrealized_pnl', header: 'Unrealized PnL', render: (row) => (
      <span style={{ color: (row.unrealized_pnl ?? 0) >= 0 ? '#16a34a' : '#dc2626', fontWeight: 600 }}>
        {row.unrealized_pnl != null ? formatNum(row.unrealized_pnl) : '-'}
      </span>
    ) },
    { key: 'product_type', header: 'Product' },
    { key: 'entry_ts', header: 'Entry Time', render: (row) => row.entry_ts ? new Date(row.entry_ts).toLocaleTimeString() : '-' },
  ], [selectedAccountId]);

  const handleExport = useCallback(() => {
    const header = 'Tenant,Account,Strategy,Contract,Net Qty,Broker Qty,Ownership,Lifecycle,Reconciliation,Mark Freshness,PnL Freshness,LTP,Unrealized PnL';
    const rows = positions.map((p) => [
      p.tenant_id || getTenantId(),
      p.broker_account_id || selectedAccountId,
      p.strategy_id || '',
      p.symbol,
      p.net_qty ?? p.quantity,
      p.broker_qty ?? '',
      p.ownership_state || '',
      p.lifecycle_state || '',
      p.reconciliation_state || '',
      p.mark_ts || '',
      p.pnl_ts || '',
      p.ltp ?? '',
      p.unrealized_pnl ?? '',
    ].join(','));
    const blob = new Blob([header + '\n' + rows.join('\n')], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'positions-evidence.csv';
    a.click();
    URL.revokeObjectURL(url);
  }, [positions, selectedAccountId]);

  const tableData = useMemo(
    () => positions.map((p) => ({ ...p } as Position & Record<string, unknown>)),
    [positions],
  );

  return (
    <div className="console-page">
      <div className="page-header">
        <div>
          <h1>Positions</h1>
          <p>Broker-observed quantities are shown beside internal authoritative records when available. Unknown is never healthy.</p>
        </div>
        {accounts.length > 1 && (
          <select
            value={selectedAccountId}
            onChange={(e) => setSelectedAccountId(e.target.value)}
            aria-label="Select broker account"
          >
            {accounts.map((a) => (
              <option key={a.broker_account_id} value={a.broker_account_id}>
                {a.display_name} ({a.trading_mode})
              </option>
            ))}
          </select>
        )}
      </div>
      {error && <StaleBanner message={error} variant="danger" />}
      {!error && isDashboardStale && (
        <StaleBanner message="Live position updates are stale. Divergence checks are not healthy until fresh evidence returns." />
      )}
      {loading ? (
        <LoadingSpinner />
      ) : (
        <DataTable
          columns={columns}
          data={tableData}
          onExport={handleExport}
          rowKey={(row) => `${row.broker_account_id || selectedAccountId}-${row.symbol}-${row.entry_ts || 'na'}`}
          emptyMessage="No open positions"
        />
      )}
    </div>
  );
};

function reconciliationBadge(row: Position & Record<string, unknown>) {
  const brokerQty = row.broker_qty;
  const netQty = row.net_qty ?? row.quantity;
  const divergent = brokerQty !== undefined && Number(brokerQty) !== Number(netQty);
  const markTs = row.mark_ts ? new Date(String(row.mark_ts)).getTime() : NaN;
  const stale = Number.isFinite(markTs) && Date.now() - markTs > 30_000;
  if (divergent) return <StatusBadge status="error" label="ACTION REQUIRED" />;
  if (stale) return <StatusBadge status="error" label="STALE MARK" />;
  return <StatusBadge status={row.reconciliation_state ? 'warning' : 'unknown'} label={String(row.reconciliation_state || 'UNKNOWN')} />;
}

function formatNum(n: number): string {
  return Number(n || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function isDashboardSnapshot(value: unknown): value is DashboardSnapshot {
  return Boolean(value && typeof value === 'object');
}

function mapHubPositionToPosition(row: HubPosition): Position {
  const quantity = Number.isFinite(row.net_qty)
    ? row.net_qty
    : row.side === 'SELL'
      ? -Math.abs(row.qty)
      : Math.abs(row.qty);
  const avgPrice = Number(row.avg_price || 0);
  const backendUnrealized = row.unrealized_pnl;
  const computedUnrealized = row.ltp != null && avgPrice > 0
    ? (row.ltp - avgPrice) * quantity
    : undefined;
  return {
    tenant_id: row.tenant_id,
    broker_account_id: row.broker_account_id,
    symbol: row.symbol,
    quantity,
    net_qty: quantity,
    qty_lots: row.side === 'SELL' ? -Math.abs(row.qty_lots) : Math.abs(row.qty_lots),
    lot_size: row.lot_size,
    avg_price: avgPrice,
    entry_price: row.entry_price,
    entry_ts: row.entry_ts,
    product_type: row.product_type,
    side: row.side,
    ltp: row.ltp ?? undefined,
    unrealized_pnl: backendUnrealized ?? computedUnrealized,
  };
}

export default Positions;
