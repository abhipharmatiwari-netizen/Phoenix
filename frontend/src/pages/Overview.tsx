import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { AdminService, createDashboardWebSocketUrl, DefaultService, OperatorHealthSummaryResponse } from '../client';
import LoadingSpinner from '../components/shared/LoadingSpinner';
import StatusBadge from '../components/shared/StatusBadge';
import { useWebSocket } from '../hooks/useWebSocket';
import { DashboardSnapshot } from '../types/dashboard';
import {
  classifyOperatorHealth,
  formatAge,
  healthReasons,
  isFreshTimestamp,
  statusClass,
  statusLabel,
} from '../lib/consoleUtils';

const Overview: React.FC = () => {
  const [healthEnvelope, setHealthEnvelope] = useState<OperatorHealthSummaryResponse | null>(null);
  const [strategyCount, setStrategyCount] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const websocketUrlFactory = useCallback(() => createDashboardWebSocketUrl('delta'), []);
  const { data: dashboardData, isStale: websocketStale } = useWebSocket(websocketUrlFactory);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [health, strategies] = await Promise.all([
        DefaultService.getOperatorHealthSummary(),
        AdminService.getStrategies().catch(() => ({ strategies: {} })),
      ]);
      setHealthEnvelope(health);
      const rawStrategies = strategies.strategies;
      setStrategyCount(
        Array.isArray(rawStrategies)
          ? rawStrategies.length
          : rawStrategies && typeof rawStrategies === 'object'
            ? Object.keys(rawStrategies as Record<string, unknown>).length
            : 0,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load overview');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 30_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const dashboard = isDashboardSnapshot(dashboardData) ? dashboardData : null;
  const health = healthEnvelope?.summary || null;
  const healthStatus = classifyOperatorHealth(health, healthEnvelope?.source || 'unavailable');
  const dashboardFresh = dashboard?.timestamp ? isFreshTimestamp(dashboard.timestamp, 15_000) : false;
  const brokerStaleCount = (health?.per_account_staleness || []).filter((item) => item.stale).length;
  const syncAges = (health?.per_account_staleness || [])
    .map((item) => item.last_sync_ts)
    .filter(Boolean) as string[];
  const newestSync = syncAges.sort().slice(-1)[0];
  const pnl = dashboard?.pnl;
  const activeStrategies = useMemo(() => {
    const selected = dashboard?.strategy_selection || [];
    const all = new Set<string>();
    selected.forEach((item) => (item.selected_strategies || []).forEach((id) => all.add(id)));
    return all.size || strategyCount || 0;
  }, [dashboard, strategyCount]);
  const reasons = healthReasons(
    health,
    healthEnvelope?.source === 'public'
      ? 'Authenticated admin diagnostics unavailable; public summary is redacted.'
      : healthEnvelope?.admin_error,
  );

  return (
    <div className="console-page">
      <div className="page-header">
        <div>
          <h1>Overview</h1>
          <p>Authenticated operator diagnostics fail closed. Unknown, stale, or public-only data is not healthy.</p>
        </div>
        <button className="secondary-button" type="button" onClick={load} disabled={loading}>
          Refresh
        </button>
      </div>

      {error && <div className="notice notice--blocked">{error}</div>}
      {healthEnvelope?.source === 'public' && (
        <div className="notice notice--warning">
          Admin diagnostics are unavailable. The public summary proves reachability only and is not used as a healthy verdict.
        </div>
      )}
      {websocketStale && <div className="notice notice--warning">Dashboard stream is stale. Freshness-dependent fields are not healthy.</div>}

      {loading && !healthEnvelope ? (
        <LoadingSpinner />
      ) : (
        <>
          <section className={`overview-verdict overview-verdict--${healthStatus}`}>
            <div>
              <span>Operator Readiness</span>
              <h2>{statusLabel(healthStatus)}</h2>
            </div>
            <span className={statusClass(healthStatus)}>
              {healthEnvelope?.source === 'admin' ? 'Admin source' : healthEnvelope?.source === 'public' ? 'Public fallback' : 'No source'}
            </span>
          </section>

          <div className="metric-grid">
            <Metric title="Operating Mode" value={health?.operating_mode || 'UNKNOWN'} status={health?.operating_mode ? 'ok' : 'unknown'} />
            <Metric title="Trade Mode" value={health?.trade_mode || dashboard?.trade_mode || 'UNKNOWN'} status={health?.trade_mode || dashboard?.trade_mode ? 'ok' : 'unknown'} />
            <Metric title="Readiness" value={health?.readiness?.ready === true ? 'Ready' : health?.readiness?.ready === false ? 'Blocked' : 'Unknown'} status={health?.readiness?.ready === true && healthStatus === 'healthy' ? 'ok' : health?.readiness?.ready === false ? 'error' : 'unknown'} />
            <Metric title="Backend Health" value={health?.status || 'Unknown'} status={healthStatus === 'healthy' ? 'ok' : healthStatus === 'blocked' ? 'error' : 'unknown'} />
            <Metric title="Dashboard Freshness" value={dashboard?.timestamp ? formatAge(dashboard.timestamp) : 'Unknown'} status={dashboardFresh && !websocketStale ? 'ok' : 'error'} />
            <Metric title="Broker Sync Age" value={newestSync ? formatAge(newestSync) : 'Unknown'} status={brokerStaleCount > 0 ? 'error' : newestSync ? 'ok' : 'unknown'} />
            <Metric title="Quote Freshness" value={dashboard?.instruments?.[0]?.tick_time ? formatAge(dashboard.instruments[0].tick_time) : 'Unknown'} status={dashboard?.instruments?.[0]?.tick_time && isFreshTimestamp(dashboard.instruments[0].tick_time, 20_000) ? 'ok' : 'unknown'} />
            <Metric title="Active Strategies" value={String(activeStrategies)} status={activeStrategies > 0 ? 'ok' : 'unknown'} />
            <Metric title="Account Count" value={String(health?.tracked_account_count ?? 'Unknown')} status={health?.tracked_account_count ? 'ok' : 'unknown'} />
            <Metric title="Current PnL" value={pnl ? `Realized ${formatMoney(pnl.realized)} / Open ${formatMoney(pnl.open)}` : 'Unknown'} status={pnl ? (pnl.invalid_marks_count ? 'warning' : 'ok') : 'unknown'} />
          </div>

          <section className="evidence-panel">
            <h2>Block Reasons</h2>
            {reasons.length ? (
              <ul className="reason-list">
                {reasons.map((reason) => <li key={reason}>{reason}</li>)}
              </ul>
            ) : (
              <p>No block reasons returned by authenticated diagnostics.</p>
            )}
          </section>
        </>
      )}
    </div>
  );
};

function Metric({
  title,
  value,
  status,
}: {
  title: string;
  value: string;
  status: 'ok' | 'warning' | 'error' | 'unknown';
}) {
  return (
    <section className="metric-card">
      <span>{title}</span>
      <strong>{value}</strong>
      <StatusBadge status={status} />
    </section>
  );
}

function isDashboardSnapshot(value: unknown): value is DashboardSnapshot {
  return Boolean(value && typeof value === 'object' && 'timestamp' in value);
}

function formatMoney(value: number): string {
  return value.toLocaleString('en-IN', { maximumFractionDigits: 0 });
}

export default Overview;
