import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { TenantService } from '../client';
import { Trade } from '../types/trading';
import DataTable, { Column } from '../components/shared/DataTable';
import LoadingSpinner from '../components/shared/LoadingSpinner';
import StaleBanner from '../components/shared/StaleBanner';

const Trades: React.FC = () => {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');
  const [strategyFilter, setStrategyFilter] = useState('all');

  const fetchTrades = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params: { from_time?: string; to_time?: string; limit?: number } = { limit: 500 };
      if (fromDate) params.from_time = new Date(fromDate).toISOString();
      if (toDate) params.to_time = new Date(toDate + 'T23:59:59').toISOString();
      const resp = await TenantService.getMyTrades(params);
      setTrades(resp.trades);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch trades');
    } finally {
      setLoading(false);
    }
  }, [fromDate, toDate]);

  useEffect(() => { fetchTrades(); }, [fetchTrades]);

  const strategies = useMemo(() => {
    const s = new Set(trades.map(t => t.strategy_id || 'unknown'));
    return ['all', ...Array.from(s).sort()];
  }, [trades]);

  const filtered = useMemo(() => {
    if (strategyFilter === 'all') return trades;
    return trades.filter(t => (t.strategy_id || 'unknown') === strategyFilter);
  }, [trades, strategyFilter]);

  const columns: Column<Trade & Record<string, unknown>>[] = useMemo(() => [
    { key: 'trade_time', header: 'Time', render: (row) => {
      const ts = row.trade_time || row.timestamp || row.entry_time;
      return ts ? new Date(ts).toLocaleString() : '—';
    }},
    { key: 'symbol', header: 'Symbol' },
    { key: 'strategy_id', header: 'Strategy', render: (row) => row.strategy_id || '—' },
    { key: 'side', header: 'Side', render: (row) => (
      <span style={{ color: row.side === 'BUY' ? '#16a34a' : '#dc2626', fontWeight: 600 }}>{row.side}</span>
    )},
    { key: 'quantity', header: 'Qty', render: (row) => String(row.quantity ?? row.qty ?? '—') },
    { key: 'price', header: 'Price', render: (row) => {
      const p = row.price ?? row.entry_price ?? row.exit_price;
      return p != null ? p.toLocaleString('en-IN', { minimumFractionDigits: 2 }) : '—';
    }},
    { key: 'pnl', header: 'PnL', render: (row) => row.pnl != null ? (
      <span style={{ color: row.pnl >= 0 ? '#16a34a' : '#dc2626', fontWeight: 600 }}>
        {row.pnl.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
      </span>
    ) : '—' },
    { key: 'order_id', header: 'Order', render: (row) => row.order_id ? (
      <span style={{ fontFamily: 'monospace', fontSize: '0.8rem' }}>{row.order_id}</span>
    ) : '—' },
  ], []);

  const handleExport = useCallback(() => {
    const header = 'Time,Symbol,Strategy,Side,Qty,Price,PnL,Order ID';
    const rows = filtered.map(t => {
      const ts = t.trade_time || t.timestamp || t.entry_time || '';
      return `${ts},${t.symbol},${t.strategy_id || ''},${t.side},${t.quantity ?? t.qty ?? ''},${t.price ?? t.entry_price ?? ''},${t.pnl ?? ''},${t.order_id || ''}`;
    });
    const blob = new Blob([header + '\n' + rows.join('\n')], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = 'trades.csv'; a.click();
    URL.revokeObjectURL(url);
  }, [filtered]);

  const tableData = useMemo(() =>
    filtered.map(t => ({ ...t } as Trade & Record<string, unknown>)),
    [filtered]
  );

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: '1rem', marginBottom: '1rem', flexWrap: 'wrap' }}>
        <h1 style={{ margin: 0 }}>Trades</h1>
        <label style={{ fontSize: '0.8rem', color: '#6b7280' }}>
          From: <input type="date" value={fromDate} onChange={e => setFromDate(e.target.value)}
            style={{ padding: '0.3rem', border: '1px solid #d1d5db', borderRadius: 4, marginLeft: 4 }}
            aria-label="From date" />
        </label>
        <label style={{ fontSize: '0.8rem', color: '#6b7280' }}>
          To: <input type="date" value={toDate} onChange={e => setToDate(e.target.value)}
            style={{ padding: '0.3rem', border: '1px solid #d1d5db', borderRadius: 4, marginLeft: 4 }}
            aria-label="To date" />
        </label>
        <select
          value={strategyFilter}
          onChange={e => setStrategyFilter(e.target.value)}
          aria-label="Filter by strategy"
          style={{ padding: '0.4rem 0.75rem', border: '1px solid #d1d5db', borderRadius: 4 }}
        >
          {strategies.map(s => (
            <option key={s} value={s}>{s === 'all' ? 'All Strategies' : s}</option>
          ))}
        </select>
      </div>
      {error && <StaleBanner message={error} variant="danger" />}
      {loading ? (
        <LoadingSpinner />
      ) : (
        <DataTable
          columns={columns}
          data={tableData}
          onExport={handleExport}
          rowKey={(row) => row.trade_id || `${row.symbol}-${row.trade_time || Math.random()}`}
          emptyMessage="No trades found"
        />
      )}
    </div>
  );
};

export default Trades;
