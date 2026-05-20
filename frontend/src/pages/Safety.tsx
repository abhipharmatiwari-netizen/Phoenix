import React, { useState, useEffect, useMemo } from 'react';
import Gate from '../auth/Gate';
import { useAuth } from '../auth/AuthContext';
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
  // PR #240 round-6 review P1: the kill-switch panel hard-codes
  // every action to ``scope: 'GLOBAL'``. A tenant-scoped admin
  // (``canAccessAllTenants === false``) using it would therefore
  // trip/clear/rearm/cancel across every tenant, not just their own.
  // Until per-tenant scoped controls exist, gate the panel on
  // ``canAccessAllTenants`` so only top-level admins can see it.
  //
  // PR #240 round-7 review P2: distinguish "entitlement still
  // loading" (``canAccessAllTenants === undefined`` because the
  // JWT-decoded fallback user has not been replaced by the
  // ``/auth/me`` response yet) from "explicit false". Otherwise a
  // legitimate global admin sees the panic-stop panel hidden
  // during the entitlement load window — exactly the period
  // during an incident when the panel is most needed. Render a
  // neutral loading placeholder while the lookup is pending, then
  // resolve to either the panel or the coordination warning.
  const { user } = useAuth();
  const entitlementUnknown = user?.canAccessAllTenants === undefined;
  const showGlobalKillSwitchPanel = user?.canAccessAllTenants === true;

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

  const isDegraded = health?.status !== 'ok' || health?.readiness?.ready === false;
  const degradedReasons = health?.degraded_reasons || [];
  const readinessReason = health?.readiness?.reason;
  const shadowIngestion = health?.oi_ml_shadow_ingestion;
  const shadowStatus = shadowIngestion?.status || 'unknown';
  const shadowRows = shadowIngestion?.option_chain?.today_row_count ?? 0;
  const shadowValidationReports = shadowIngestion?.validation_reports?.today_report_count ?? 0;

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

        {/* PR #240 round-6 review P1: render KillSwitchPanel
            OUTSIDE the health-fetch error branch so a failing
            ``/health/summary`` (e.g. timing out during an incident)
            does not also hide the dashboard panic-stop path. The
            panel fetches its own state from ``/admin/kill-switch/state``
            and is independent of health/audit. Admin role + global
            tenant entitlement (``canAccessAllTenants``) both required
            because every action targets ``scope: 'GLOBAL'``. */}
        <Gate requiredRoles={[Role.ADMIN]}>
          {showGlobalKillSwitchPanel ? (
            <KillSwitchPanel />
          ) : entitlementUnknown ? (
            <div style={{
              border: '1px solid #9ca3af',
              borderRadius: 8,
              padding: '0.75rem 1rem',
              backgroundColor: '#f3f4f6',
              color: '#374151',
              fontSize: '0.875rem',
              marginBottom: '1.5rem',
            }}>
              <strong>Resolving admin entitlement…</strong>{' '}
              The Global kill-switch panel will appear once your
              cross-tenant entitlement is confirmed via /auth/me. If
              this persists, refresh the page or sign in again.
            </div>
          ) : (
            <div style={{
              border: '1px solid #f59e0b',
              borderRadius: 8,
              padding: '0.75rem 1rem',
              backgroundColor: '#fffbeb',
              color: '#92400e',
              fontSize: '0.875rem',
              marginBottom: '1.5rem',
            }}>
              <strong>Global kill-switch controls hidden.</strong>{' '}
              These controls operate on the GLOBAL scope and require
              an admin with cross-tenant entitlement. A tenant-scoped
              admin must coordinate with a global admin to trip /
              clear / rearm. Per-tenant scoped controls are not yet
              implemented (see issue #238 follow-up).
            </div>
          )}
        </Gate>

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
                label={(readinessReason || health?.status || 'UNKNOWN').toUpperCase()}
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
              <Card title="OI/ML Shadow">
                <StatusBadge
                  status={shadowStatus === 'ok' || shadowStatus === 'disabled' ? 'ok' : 'error'}
                  label={shadowStatus.toUpperCase()}
                />
                <div style={{ marginTop: '0.5rem', fontSize: '0.75rem', color: '#4b5563' }}>
                  Rows {shadowRows} / Reports {shadowValidationReports}
                  {shadowIngestion?.reason ? ` / ${shadowIngestion.reason}` : ''}
                </div>
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
