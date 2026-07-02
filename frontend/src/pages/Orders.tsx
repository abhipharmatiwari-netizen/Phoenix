import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { TenantService } from '../client';
import { BrokerAccount, Order } from '../types/trading';
import DataTable, { Column } from '../components/shared/DataTable';
import StatusBadge from '../components/shared/StatusBadge';
import LoadingSpinner from '../components/shared/LoadingSpinner';
import StaleBanner from '../components/shared/StaleBanner';
import OrderTimeline from '../components/shared/OrderTimeline';

type OrderRow = Order & Record<string, unknown>;

const STATUS_VARIANT: Record<string, 'ok' | 'warning' | 'error' | 'info' | 'unknown'> = {
  complete: 'ok',
  completed: 'ok',
  filled: 'ok',
  open: 'info',
  pending: 'info',
  submitted: 'info',
  partial_fill: 'warning',
  cancelled: 'unknown',
  canceled: 'unknown',
  rejected: 'error',
  failed: 'error',
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
    return orders.filter((order) => String(order.status || '').toLowerCase() === statusFilter);
  }, [orders, statusFilter]);

  const statuses = useMemo(() => {
    const statusSet = new Set(orders.map((order) => String(order.status || '').toLowerCase()).filter(Boolean));
    return ['all', ...Array.from(statusSet).sort()];
  }, [orders]);

  const columns: Column<OrderRow>[] = useMemo(() => [
    { key: 'order_id', header: 'Internal Order ID', render: (row) => (
      <span style={{ fontFamily: 'monospace', fontSize: '0.8rem' }}>{row.internal_order_id || row.order_id}</span>
    ) },
    { key: 'broker_order_id', header: 'Broker Order ID', render: (row) => row.broker_order_id || row.order_id || 'Unknown' },
    { key: 'strategy_id', header: 'Strategy', render: (row) => row.strategy_id || 'Unknown' },
    { key: 'symbol', header: 'Symbol' },
    { key: 'side', header: 'Side', render: (row) => (
      <span style={{ color: row.side === 'BUY' ? '#16a34a' : '#dc2626', fontWeight: 600 }}>{row.side}</span>
    ) },
    { key: 'status', header: 'Status', render: (row) => {
      const status = String(row.status || 'unknown');
      return <StatusBadge status={STATUS_VARIANT[status.toLowerCase()] || 'unknown'} label={status} />;
    } },
    { key: 'lifecycle_state', header: 'Lifecycle', render: (row) => row.lifecycle_state || row.status || 'Unknown' },
    { key: 'outbox_state', header: 'Outbox', render: (row) => row.outbox_state || 'Unknown' },
    { key: 'order_type', header: 'Type' },
    { key: 'quantity', header: 'Qty' },
    { key: 'filled_qty', header: 'Filled', render: (row) => String(row.filled_quantity ?? row.filled_qty ?? '-') },
    { key: 'exit_reason', header: 'Exit Reason', render: (row) => row.exit_reason || 'Unknown' },
    { key: 'retry_state', header: 'Retry', render: (row) => row.retry_state || 'Unknown' },
    { key: 'idempotency_key', header: 'Idempotency', render: (row) => row.idempotency_key ? 'Present' : 'Unknown' },
    { key: 'price', header: 'Price', render: (row) => row.price ? row.price.toLocaleString('en-IN', { minimumFractionDigits: 2 }) : '-' },
    { key: 'created_at', header: 'Created', render: (row) => row.created_at ? new Date(row.created_at).toLocaleString() : '-' },
  ], []);

  const handleRowClick = useCallback((row: OrderRow) => {
    setExpandedOrderId((prev) => prev === row.order_id ? null : row.order_id);
  }, []);

  const handleExport = useCallback(() => {
    const header = 'Internal Order ID,Broker Order ID,Strategy,Symbol,Side,Status,Lifecycle,Outbox,Exit Reason,Retry,Idempotency,Created';
    const rows = filtered.map((order) => [
      order.internal_order_id || order.order_id || '',
      order.broker_order_id || order.order_id || '',
      order.strategy_id || '',
      order.symbol,
      order.side,
      order.status,
      order.lifecycle_state || '',
      order.outbox_state || '',
      order.exit_reason || '',
      order.retry_state || '',
      order.idempotency_key ? 'present' : '',
      order.created_at || '',
    ].join(','));
    const blob = new Blob([header + '\n' + rows.join('\n')], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'orders-evidence.csv';
    a.click();
    URL.revokeObjectURL(url);
  }, [filtered]);

  const tableData: OrderRow[] = useMemo(() => filtered.map((order) => ({ ...order } as OrderRow)), [filtered]);

  return (
    <div className="console-page">
      <div className="page-header">
        <div>
          <h1>Orders</h1>
          <p>Read-only order lifecycle, outbox, broker IDs, retry, exit reason, and idempotency evidence.</p>
        </div>
        <div className="toolbar">
          {accounts.length > 1 && (
            <select value={selectedAccountId} onChange={(e) => setSelectedAccountId(e.target.value)} aria-label="Select broker account">
              {accounts.map((account) => (
                <option key={account.broker_account_id} value={account.broker_account_id}>
                  {account.display_name} ({account.trading_mode})
                </option>
              ))}
            </select>
          )}
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} aria-label="Filter by status">
            {statuses.map((status) => (
              <option key={status} value={status}>{status === 'all' ? 'All Statuses' : status.toUpperCase()}</option>
            ))}
          </select>
        </div>
      </div>
      {error && <StaleBanner message={error} variant="danger" />}
      {loading ? (
        <LoadingSpinner />
      ) : (
        <>
          <DataTable
            columns={columns}
            data={tableData}
            onExport={handleExport}
            onRowClick={handleRowClick}
            rowKey={(row) => row.order_id}
            emptyMessage="No orders found"
          />
          {expandedOrderId && (
            <div className="evidence-panel">
              <h2>Order Lifecycle: {expandedOrderId}</h2>
              <OrderTimeline events={orders.find((order) => order.order_id === expandedOrderId)?.events || []} />
            </div>
          )}
        </>
      )}
    </div>
  );
};

export default Orders;
