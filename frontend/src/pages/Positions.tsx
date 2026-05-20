import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { TenantService, createDashboardWebSocketUrl, getTenantId } from '../client';
import { BrokerAccount, Position } from '../types/trading';
import { DashboardSnapshot, HubPosition } from '../types/dashboard';
import DataTable, { Column } from '../components/shared/DataTable';
import LoadingSpinner from '../components/shared/LoadingSpinner';
import StaleBanner from '../components/shared/StaleBanner';
import { useWebSocket } from '../hooks/useWebSocket';

const Positions: React.FC = () => {
  const [accounts, setAccounts] = useState<BrokerAccount[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState<string>('');
  const [positions, setPositions] = useState<Position[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const websocketUrlFactory = useCallback(
    () => createDashboardWebSocketUrl('delta'),
    [],
  );
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
      .filter((row) =>
        row.broker_account_id === selectedAccountId
        && row.tenant_id === tenantId
      )
      .map(mapHubPositionToPosition);
    setPositions(livePositions);
    setLoading(false);
  }, [dashboardData, selectedAccountId]);

  const columns: Column<Position & Record<string, unknown>>[] = useMemo(() => [
    { key: 'symbol', header: 'Symbol' },
    { key: 'side', header: 'Side', render: (row) => (
      <span style={{ color: row.side === 'BUY' ? '#16a34a' : '#dc2626', fontWeight: 600 }}>
        {row.side || '—'}
      </span>
    )},
    { key: 'quantity', header: 'Qty' },
    { key: 'avg_price', header: 'Avg Price', render: (row) => formatNum(row.avg_price) },
    { key: 'ltp', header: 'LTP', render: (row) => row.ltp != null ? formatNum(row.ltp) : '—' },
    { key: 'unrealized_pnl', header: 'Unrealized PnL', render: (row) => (
      <span style={{ color: (row.unrealized_pnl ?? 0) >= 0 ? '#16a34a' : '#dc2626', fontWeight: 600 }}>
        {row.unrealized_pnl != null ? formatNum(row.unrealized_pnl) : '—'}
      </span>
    )},
    { key: 'product_type', header: 'Product' },
    { key: 'entry_ts', header: 'Entry Time', render: (row) => row.entry_ts ? new Date(row.entry_ts).toLocaleTimeString() : '—' },
  ], []);

  const handleExport = useCallback(() => {
    const header = 'Symbol,Side,Qty,Avg Price,LTP,Unrealized PnL,Product,Entry Time';
    const rows = positions.map(p =>
      `${p.symbol},${p.side || ''},${p.quantity},${p.avg_price},${p.ltp ?? ''},${p.unrealized_pnl ?? ''},${p.product_type},${p.entry_ts || ''}`
    );
    const blob = new Blob([header + '\n' + rows.join('\n')], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'positions.csv'; a.click();
    URL.revokeObjectURL(url);
  }, [positions]);

  const tableData = useMemo(() =>
    positions.map(p => ({ ...p } as Position & Record<string, unknown>)),
    [positions]
  );

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: '1rem', marginBottom: '1rem' }}>
        <h1 style={{ margin: 0 }}>Positions</h1>
        {accounts.length > 1 && (
          <select
            value={selectedAccountId}
            onChange={(e) => setSelectedAccountId(e.target.value)}
            aria-label="Select broker account"
            style={{ padding: '0.4rem 0.75rem', border: '1px solid #d1d5db', borderRadius: 4 }}
          >
            {accounts.map(a => (
              <option key={a.broker_account_id} value={a.broker_account_id}>
                {a.display_name} ({a.trading_mode})
              </option>
            ))}
          </select>
        )}
      </div>
      {error && <StaleBanner message={error} variant="danger" />}
      {!error && isDashboardStale && (
        <StaleBanner message="Live position updates are temporarily stale. Showing the last known snapshot." />
      )}
      {loading ? (
        <LoadingSpinner />
      ) : (
        <DataTable
          columns={columns}
          data={tableData}
          onExport={handleExport}
          rowKey={(row) => `${row.symbol}-${row.entry_ts || 'na'}`}
          emptyMessage="No open positions"
        />
      )}
    </div>
  );
};

function formatNum(n: number): string {
  return n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
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
