import React, { useState, useEffect, useMemo } from 'react';
import Gate from '../auth/Gate';
import { Role } from '../lib/rbac';
import { DefaultService, AuditService } from '../client';
import { HealthSummary } from '../types/health';
import { AuditEvent } from '../types/trading';
import StatusBadge from '../components/shared/StatusBadge';
import DataTable, { Column } from '../components/shared/DataTable';
import LoadingSpinner from '../components/shared/LoadingSpinner';
import Card from '../components/shared/Card';

const Safety: React.FC = () => {
  const [health, setHealth] = useState<HealthSummary | null>(null);
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const fetchData = async () => {
      try {
        const [healthResp, auditResp] = await Promise.all([
          DefaultService.getHealthSummary(),
          AuditService.getAuditLog({ action: 'break_glass', limit: 20 }).catch(() => ({ events: [], count: 0 })),
        ]);
        if (active) {
          setHealth(healthResp);
          setAuditEvents(auditResp.events || []);
        }
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : 'Failed to fetch safety data');
      } finally {
        if (active) setLoading(false);
      }
    };
    fetchData();
    const timer = setInterval(fetchData, 30_000);
    return () => { active = false; clearInterval(timer); };
  }, []);

  const isDegraded = health?.status !== 'ok';
  const degradedReasons = health?.degraded_reasons || [];

  const auditColumns: Column<AuditEvent & Record<string, unknown>>[] = useMemo(() => [
    { key: 'timestamp', header: 'Time', render: (row) => new Date(row.timestamp).toLocaleString() },
    { key: 'actor', header: 'Actor' },
    { key: 'action', header: 'Action' },
    { key: 'resource_type', header: 'Resource' },
    { key: 'resource_id', header: 'Resource ID' },
  ], []);

  const auditData = useMemo(() =>
    auditEvents.map(e => ({ ...e } as AuditEvent & Record<string, unknown>)),
    [auditEvents]
  );

  return (
    <Gate requiredRoles={[Role.OPERATOR, Role.ADMIN]}>
      <div>
        <h1>Safety & Emergency Controls</h1>

        {loading ? <LoadingSpinner /> : error ? (
          <div style={{ color: '#dc2626', padding: '1rem' }}>{error}</div>
        ) : (
          <>
            {/* System Status Card */}
            <div style={{
              border: `2px solid ${isDegraded ? '#dc2626' : '#16a34a'}`,
              borderRadius: 12,
              padding: '1.5rem',
              marginBottom: '1.5rem',
              backgroundColor: isDegraded ? '#fef2f2' : '#f0fdf4',
              textAlign: 'center',
            }}>
              <div style={{
                fontSize: '1.75rem',
                fontWeight: 700,
                color: isDegraded ? '#dc2626' : '#16a34a',
                marginBottom: '0.5rem',
              }}>
                {isDegraded ? '⚠ SYSTEM DEGRADED' : '✓ SYSTEM ACTIVE'}
              </div>
              <StatusBadge
                status={isDegraded ? 'error' : 'ok'}
                label={health?.status?.toUpperCase() || 'UNKNOWN'}
              />
              {degradedReasons.length > 0 && (
                <ul style={{ textAlign: 'left', maxWidth: 500, margin: '1rem auto', color: '#dc2626', fontSize: '0.875rem' }}>
                  {degradedReasons.map((r, i) => <li key={i}>{r}</li>)}
                </ul>
              )}
            </div>

            {/* System Details */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '0.75rem', marginBottom: '1.5rem' }}>
              <Card title="Stream Worker">
                <StatusBadge status={health?.stream_worker_running ? 'ok' : 'error'} label={health?.stream_worker_running ? 'Running' : 'Stopped'} />
              </Card>
              <Card title="Watchdog">
                <StatusBadge status={health?.watchdog_running ? 'ok' : 'error'} label={health?.watchdog_running ? 'Running' : 'Stopped'} />
              </Card>
              <Card title="Schema">
                <StatusBadge status={health?.schema_status === 'ok' ? 'ok' : 'warning'} label={health?.schema_status || 'unknown'} />
              </Card>
              <Card title="Tracked Accounts">
                <span style={{ fontSize: '1.5rem', fontWeight: 700 }}>{health?.tracked_account_count ?? 0}</span>
              </Card>
            </div>

            {/* Safety Note */}
            <div style={{
              border: '1px solid #fcd34d',
              borderRadius: 8,
              padding: '1rem',
              backgroundColor: '#fffbeb',
              marginBottom: '1.5rem',
              fontSize: '0.875rem',
              color: '#92400e',
            }}>
              <strong>Note:</strong> Kill switch and break-glass controls are enforced server-side via the maintenance manager.
              Strategy toggles are available from the <a href="/control-tower" style={{ color: '#2563eb' }}>Control Tower</a>.
              For emergency shutdown, use backend ops procedures or the break-glass CLI tool.
            </div>

            {/* Audit Trail */}
            <h2 style={{ fontSize: '1rem', color: '#374151', marginBottom: '0.5rem' }}>Safety Audit Trail</h2>
            <DataTable
              columns={auditColumns}
              data={auditData}
              sortable={true}
              filterable={false}
              rowKey={(row) => `${row.timestamp}-${row.action}-${row.resource_id}`}
              emptyMessage="No safety-related audit events"
            />
          </>
        )}
      </div>
    </Gate>
  );
};

export default Safety;
