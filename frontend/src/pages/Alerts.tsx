import React, { useEffect, useMemo, useState } from 'react';
import { DefaultService } from '../client';
import DataTable, { Column } from '../components/shared/DataTable';
import LoadingSpinner from '../components/shared/LoadingSpinner';
import StatusBadge from '../components/shared/StatusBadge';
import { AlertRule } from '../types/health';

type AlertRow = AlertRule & Record<string, unknown>;

function timestamp(value: number | null | undefined): string {
  if (!value) {
    return 'Unknown';
  }
  const millis = value > 10_000_000_000 ? value : value * 1000;
  return new Date(millis).toLocaleString();
}

const Alerts: React.FC = () => {
  const [alerts, setAlerts] = useState<AlertRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    const fetchAlerts = async () => {
      try {
        const response = await DefaultService.getHealthAlerts();
        if (active) {
          setAlerts(Array.isArray(response?.alerts) ? response.alerts : []);
        }
      } catch (err) {
        if (active) {
          setError(err instanceof Error ? err.message : 'Failed to fetch alerts');
        }
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    };

    fetchAlerts();
    const timer = window.setInterval(fetchAlerts, 30_000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  const columns: Column<AlertRow>[] = useMemo(() => [
    { key: 'severity', header: 'Severity', render: (row) => (
      <StatusBadge
        status={row.severity === 'critical' ? 'error' : row.severity === 'warning' ? 'warning' : 'info'}
        label={String(row.severity || 'unknown').toUpperCase()}
      />
    ) },
    { key: 'rule_name', header: 'Rule' },
    { key: 'state', header: 'State', render: (row) => (
      <StatusBadge status={row.state === 'firing' ? 'error' : row.state === 'ok' ? 'ok' : 'unknown'} label={String(row.state || 'unknown')} />
    ) },
    { key: 'message', header: 'Trigger Condition', render: (row) => row.message || row.value || 'Unknown' },
    { key: 'fired_at', header: 'Timestamp', render: (row) => timestamp(row.fired_at) },
    { key: 'labels', header: 'Affected Scope', render: (row) => {
      const labels = row.labels || {};
      return Object.keys(labels).length ? JSON.stringify(labels) : 'Global';
    } },
    { key: 'recommended_action', header: 'Recommended Action', render: () => 'Review Safety and correlated audit evidence before resuming operations.' },
  ], []);

  const data = useMemo<AlertRow[]>(() => alerts.map((alert) => ({ ...alert })), [alerts]);

  return (
    <div className="console-page">
      <div className="page-header">
        <div>
          <h1>Alerts</h1>
          <p>Current alert-rule state from /health/alerts. This endpoint must return JSON, never SPA HTML.</p>
        </div>
      </div>
      {error && <div className="notice notice--blocked">{error}</div>}
      {loading ? (
        <LoadingSpinner />
      ) : (
        <DataTable columns={columns} data={data} emptyMessage="No alert rules are firing." />
      )}
    </div>
  );
};

export default Alerts;
