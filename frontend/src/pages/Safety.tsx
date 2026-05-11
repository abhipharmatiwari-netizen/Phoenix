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
import KillSwitchPanel from '../components/KillSwitchPanel';

const Safety: React.FC = () => {
  const [health, setHealth] = useState<HealthSummary | null>(null);
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const fetchData = async () => {
      try {
        const [healthResp, breakGlassResp, killSwitchResp, cancelAllResp] = await Promise.all([
          DefaultService.getHealthSummary(),
          AuditService.getAuditLog({ action: 'break_glass', limit: 20 }).catch(() => ({ events: [], count: 0 })),
          // Issue #238: surface every kill-switch toggle attempt in the
          // audit table so operators see who tripped/cleared/rearmed and
          // what reason was recorded.
          AuditService.getAuditLog({ resource_type: 'kill_switch', limit: 50 }).catch(() => ({ events: [], count: 0 })),
          // PR #240 round-1 review P2: cancel-all emits
          // ``resource_type=broker_orders`` (not kill_switch) so it must
          // be queried separately; otherwise bulk cancel attempts are
          // missing from the Safety Audit Trail even though the runbook
          // says they are merged there.
          AuditService.getAuditLog({ resource_type: 'broker_orders', limit: 50 }).catch(() => ({ events: [], count: 0 })),
        ]);
        if (active) {
          setHealth(healthResp);
          // Merge the audit feeds; de-duplicate by audit_id when
          // present, otherwise by timestamp+action+resource_id.
          const merged: AuditEvent[] = [];
          const seen = new Set<string>();
          [
            ...(killSwitchResp.events || []),
            ...(cancelAllResp.events || []),
            ...(breakGlassResp.events || []),
          ].forEach((e) => {
            const key = (e as { audit_id?: string }).audit_id
              || `${e.timestamp}|${e.action}|${e.resource_id}`;
            if (seen.has(key)) return;
            seen.add(key);
            merged.push(e);
          });
          merged.sort((a, b) => (b.timestamp || '').localeCompare(a.timestamp || ''));
          setAuditEvents(merged);
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

            {/* Issue #238: Kill-switch controls — admin only.
                Operators see a read-only state line via the audit table; only
                admins see the trip/clear/rearm/cancel-all buttons. The panel
                fetches its own state via /admin/kill-switch/state and refreshes
                after every action so the UI cannot drift from backend. */}
            <Gate requiredRoles={[Role.ADMIN]}>
              <KillSwitchPanel />
            </Gate>

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
              <strong>Note:</strong> Kill switch is enforced server-side via the
              durable <code>KillSwitchManager</code>. Admins use the controls above
              to trip, clear, rearm, and cancel open broker orders. Strategy toggles
              are available from the <a href="/control-tower" style={{ color: '#2563eb' }}>Control Tower</a>.
              For emergency operator-initiated panic stops, see the runbook at
              <code>docs/runbooks/dashboard-kill-switch.md</code>.
            </div>

            {/* Audit Trail (kill-switch + break-glass) */}
            <h2 style={{ fontSize: '1rem', color: '#374151', marginBottom: '0.5rem' }}>
              Safety Audit Trail (Kill Switch &amp; Break-Glass)
            </h2>
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
