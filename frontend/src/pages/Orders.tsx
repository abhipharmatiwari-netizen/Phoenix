import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { TenantService } from '../client';
import { BrokerAccount, Order } from '../types/trading';
import DataTable, { Column } from '../components/shared/DataTable';
import StatusBadge from '../components/shared/StatusBadge';
import LoadingSpinner from '../components/shared/LoadingSpinner';
import StaleBanner from '../components/shared/StaleBanner';
import OrderTimeline from '../components/shared/OrderTimeline';

type OrderRow = Order & Record<string, unknown>;

const STATUS_VARIANT: Record<string, 'ok' | 'warning' | 'error' | 'info' | 'unknown'> = {
  complete: 'ok', filled: 'ok',
  open: 'info', pending: 'info', submitted: 'info',
  partial_fill: 'warning',
  cancelled: 'unknown',
  rejected: 'error', failed: 'error',
};

const Orders: React.FC = () => {
  const [accounts, setAccounts] = useState<BrokerAccount[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState('');
  const [orders, setOrders] = useState<Order[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedOrderId, setExpandedOrderId] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState('all');

  useEffect(() => {
    let active = true;
    const fetch = async () => {
      try {
        const resp = await TenantService.listMyAccounts();
        if (active) {
          setAccounts(resp.accounts);
          if (resp.accounts.length > 0) setSelectedAccountId(resp.accounts[0].broker_account_id);
        }
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : 'Failed to fetch accounts');
      } finally {
        if (active) setLoading(false);
      }
    };
    fetch();
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!selectedAccountId) return;
    let active = true;
    setLoading(true);
    const fetchOrders = async () => {
      try {
        const resp = await TenantService.getAccountOrders(selectedAccountId);
        if (active) setOrders(resp.orders);
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : 'Failed to fetch orders');
      } finally {
        if (active) setLoading(false);
      }
    };
    fetchOrders();
    return () => { active = false; };
  }, [selectedAccountId]);

  const filtered = useMemo(() => {
    if (statusFilter === 'all') return orders;
    return orders.filter(o => o.status.toLowerCase() === statusFilter);
  }, [orders, statusFilter]);

  const statuses = useMemo(() => {
    const s = new Set(orders.map(o => o.status.toLowerCase()));
    return ['all', ...Array.from(s).sort()];
  }, [orders]);

  const columns: Column<OrderRow>[] = useMemo(() => [
    { key: 'order_id', header: 'Order ID', render: (row) => (
      <span style={{ fontFamily: 'monospace', fontSize: '0.8rem' }}>{row.order_id}</span>
    )},
    { key: 'symbol', header: 'Symbol' },
    { key: 'side', header: 'Side', render: (row) => (
      <span style={{ color: row.side === 'BUY' ? '#16a34a' : '#dc2626', fontWeight: 600 }}>{row.side}</span>
    )},
    { key: 'status', header: 'Status', render: (row) => (
      <StatusBadge status={STATUS_VARIANT[row.status.toLowerCase()] || 'unknown'} label={row.status} />
    )},
    { key: 'order_type', header: 'Type' },
    { key: 'quantity', header: 'Qty' },
    { key: 'filled_qty', header: 'Filled', render: (row) => String(row.filled_quantity ?? row.filled_qty ?? '—') },
    { key: 'price', header: 'Price', render: (row) => row.price ? row.price.toLocaleString('en-IN', { minimumFractionDigits: 2 }) : '—' },
    { key: 'created_at', header: 'Created', render: (row) => row.created_at ? new Date(row.created_at).toLocaleString() : '—' },
  ], []);

  const handleRowClick = useCallback((row: OrderRow) => {
    setExpandedOrderId(prev => prev === row.order_id ? null : row.order_id);
  }, []);

  const tableData: OrderRow[] = useMemo(() =>
    filtered.map(o => ({ ...o } as OrderRow)),
    [filtered]
  );

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: '1rem', marginBottom: '1rem', flexWrap: 'wrap' }}>
        <h1 style={{ margin: 0 }}>Orders</h1>
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
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          aria-label="Filter by status"
          style={{ padding: '0.4rem 0.75rem', border: '1px solid #d1d5db', borderRadius: 4 }}
        >
          {statuses.map(s => (
            <option key={s} value={s}>{s === 'all' ? 'All Statuses' : s.toUpperCase()}</option>
          ))}
        </select>
      </div>
      {error && <StaleBanner message={error} variant="danger" />}
      {loading ? (
        <LoadingSpinner />
      ) : (
        <>
          <DataTable
            columns={columns}
            data={tableData}
            onRowClick={handleRowClick}
            rowKey={(row) => row.order_id}
            emptyMessage="No orders found"
          />
          {expandedOrderId && (
            <div style={{ border: '1px solid #e5e7eb', borderRadius: 8, margin: '0.5rem 0', padding: '0.5rem', backgroundColor: '#f9fafb' }}>
              <h4 style={{ margin: '0 0 0.5rem 0.5rem', fontSize: '0.85rem', color: '#374151' }}>
                Order Lifecycle — {expandedOrderId}
              </h4>
              <OrderTimeline events={orders.find(o => o.order_id === expandedOrderId)?.events || []} />
            </div>
          )}
        </>
      )}
    </div>
  );
};

export default Orders;
