import React, { useEffect, useMemo, useState } from 'react';
import { DefaultService } from '../client';
import DataTable, { Column } from '../components/shared/DataTable';
import LoadingSpinner from '../components/shared/LoadingSpinner';
import StatusBadge from '../components/shared/StatusBadge';
import { MitigationEvent, MitigationsResponse } from '../types/health';

type MitigationRow = MitigationEvent & Record<string, unknown>;

function eventTimestamp(value: number): string {
  if (!value) {
    return 'Unknown';
  }
  const millis = value > 10_000_000_000 ? value : value * 1000;
  return new Date(millis).toLocaleString();
}

const Mitigations: React.FC = () => {
  const [mitigations, setMitigations] = useState<MitigationsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    const fetchMitigations = async () => {
      try {
        const response = await DefaultService.getHealthMitigations();
        if (active) {
          setMitigations({
            enabled: Boolean(response?.enabled),
            rule_count: Number(response?.rule_count ?? 0),
            total_events: Number(response?.total_events ?? 0),
            recent_events: Array.isArray(response?.recent_events) ? response.recent_events : [],
            fault_counts: response?.fault_counts && typeof response.fault_counts === 'object'
              ? response.fault_counts
              : {},
          });
        }
      } catch (err) {
        if (active) {
          setError(err instanceof Error ? err.message : 'Failed to fetch mitigations');
        }
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    };

    fetchMitigations();
    const timer = window.setInterval(fetchMitigations, 30_000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  const columns: Column<MitigationRow>[] = useMemo(() => [
    { key: 'rule_name', header: 'Rule' },
    { key: 'action', header: 'Action', render: (row) => <StatusBadge status="warning" label={String(row.action || 'unknown')} /> },
    { key: 'scope_key', header: 'Affected Scope', render: (row) => row.scope_key ? `${row.scope_key}:${row.scope_value}` : 'Global' },
    { key: 'fault_count', header: 'Fault Count' },
    { key: 'timestamp', header: 'Timestamp', render: (row) => eventTimestamp(Number(row.timestamp || 0)) },
    { key: 'details', header: 'Trigger Condition', render: (row) => row.details ? JSON.stringify(row.details) : 'Rule threshold exceeded' },
    { key: 'recommended_action', header: 'Recommended Action', render: () => 'Inspect Safety, Positions, and Audit before clearing degraded state.' },
  ], []);

  const data = useMemo<MitigationRow[]>(
    () => (mitigations?.recent_events || []).map((event) => ({ ...event })),
    [mitigations],
  );

  return (
    <div className="console-page">
      <div className="page-header">
        <div>
          <h1>Mitigations</h1>
          <p>Auto-mitigation events from /health/mitigations. This endpoint must return JSON, never SPA HTML.</p>
        </div>
        {mitigations && (
          <StatusBadge status={mitigations.enabled ? 'ok' : 'unknown'} label={mitigations.enabled ? 'Enabled' : 'Disabled'} />
        )}
      </div>

      {error && <div className="notice notice--blocked">{error}</div>}
      {loading ? (
        <LoadingSpinner />
      ) : (
        <>
          <div className="metric-grid">
            <section className="metric-card">
              <span>Rules</span>
              <strong>{mitigations?.rule_count ?? 0}</strong>
            </section>
            <section className="metric-card">
              <span>Total Events</span>
              <strong>{mitigations?.total_events ?? 0}</strong>
            </section>
          </div>
          <div className="table-scroll">
            <DataTable columns={columns} data={data} emptyMessage="No mitigation events have been recorded." />
          </div>
          <section className="evidence-panel">
            <h2>Fault Counts</h2>
            <pre className="json-block">{JSON.stringify(mitigations?.fault_counts || {}, null, 2)}</pre>
          </section>
        </>
      )}
    </div>
  );
};

export default Mitigations;
