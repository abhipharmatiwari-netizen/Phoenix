import React, { useCallback, useEffect, useMemo, useState } from 'react';
import Gate from '../auth/Gate';
import { AuditService } from '../client';
import DataTable, { Column } from '../components/shared/DataTable';
import LoadingSpinner from '../components/shared/LoadingSpinner';
import StatusBadge from '../components/shared/StatusBadge';
import { compactJson, formatDateTime } from '../lib/consoleUtils';
import { Role } from '../lib/rbac';
import { AuditEvent } from '../types/trading';

type AuditRow = AuditEvent & Record<string, unknown>;

const ACTION_TONE = (action: string): 'ok' | 'warning' | 'error' | 'info' | 'unknown' => {
  const token = action.toLowerCase();
  if (token.includes('failed') || token.includes('break_glass') || token.includes('kill_switch')) {
    return token.includes('rearm') || token.includes('clear') ? 'warning' : 'error';
  }
  if (token.includes('login') || token.includes('release')) {
    return 'info';
  }
  return 'unknown';
};

const Audit: React.FC = () => {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionFilter, setActionFilter] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await AuditService.getAuditLog({
        action: actionFilter || undefined,
        limit: 200,
      });
      setEvents(response.events || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load audit events');
    } finally {
      setLoading(false);
    }
  }, [actionFilter]);

  useEffect(() => {
    load();
  }, [load]);

  const columns: Column<AuditRow>[] = useMemo(() => [
    { key: 'timestamp', header: 'Time', render: (row) => formatDateTime(row.timestamp) },
    { key: 'actor', header: 'Actor' },
    {
      key: 'action',
      header: 'Action',
      render: (row) => <StatusBadge status={ACTION_TONE(String(row.action || ''))} label={String(row.action || 'unknown')} />,
    },
    { key: 'resource_type', header: 'Resource' },
    { key: 'resource_id', header: 'Resource ID' },
    {
      key: 'evidence',
      header: 'Evidence',
      sortable: false,
      render: (row) => (
        <details>
          <summary>View</summary>
          <pre className="evidence-block">
            {compactJson({
              before: row.before,
              after: row.after,
              metadata: row.metadata,
              request_id: row.request_id,
            })}
          </pre>
        </details>
      ),
    },
  ], []);

  const data = useMemo<AuditRow[]>(() => events.map((event) => ({ ...event })), [events]);

  return (
    <Gate requiredRoles={[Role.READONLY]}>
      <div className="console-page">
        <div className="page-header">
          <div>
            <h1>Audit</h1>
            <p>Admin actions, safety events, login/logout, strategy changes, credential rotation, and release evidence reads.</p>
          </div>
          <div className="toolbar">
            <input
              aria-label="Audit action filter"
              placeholder="Action filter"
              value={actionFilter}
              onChange={(event) => setActionFilter(event.target.value)}
            />
            <button className="secondary-button" type="button" onClick={load} disabled={loading}>
              Refresh
            </button>
          </div>
        </div>

        {error && <div className="notice notice--blocked">{error}</div>}
        {loading ? (
          <LoadingSpinner />
        ) : (
          <DataTable
            columns={columns}
            data={data}
            rowKey={(row) => `${row.timestamp}-${row.action}-${row.resource_id}`}
            emptyMessage="No audit events returned"
          />
        )}
      </div>
    </Gate>
  );
};

export default Audit;
