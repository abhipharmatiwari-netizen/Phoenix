import React, { useEffect, useState } from 'react';
import Gate from '../auth/Gate';
import { useAuth } from '../auth/AuthContext';
import { DefaultService } from '../client';
import StatusBadge from '../components/shared/StatusBadge';
import { classifyOperatorHealth, formatDateTime } from '../lib/consoleUtils';
import { Role } from '../lib/rbac';
import { HealthSummary } from '../types/health';

const Settings: React.FC = () => {
  const { user } = useAuth();
  const [health, setHealth] = useState<HealthSummary | null>(null);
  const [source, setSource] = useState<'admin' | 'public' | 'unavailable'>('unavailable');

  useEffect(() => {
    let active = true;
    DefaultService.getOperatorHealthSummary()
      .then((response) => {
        if (active) {
          setHealth(response.summary);
          setSource(response.source);
        }
      })
      .catch(() => {
        if (active) {
          setSource('unavailable');
        }
      });
    return () => {
      active = false;
    };
  }, []);

  const status = classifyOperatorHealth(health, source);

  return (
    <Gate requiredRoles={[Role.READONLY]}>
      <div className="console-page">
        <div className="page-header">
          <div>
            <h1>Settings</h1>
            <p>Session, access scope, and security controls visible to this browser session.</p>
          </div>
        </div>

        <div className="settings-grid">
          <section className="evidence-panel">
            <h2>Session</h2>
            <dl className="detail-list">
              <dt>User</dt>
              <dd>{user?.email || 'Unknown'}</dd>
              <dt>Role</dt>
              <dd>{user?.role || 'Unknown'}</dd>
              <dt>Tenant scope</dt>
              <dd>{user?.canAccessAllTenants ? 'All tenants' : (user?.tenantIds || []).join(', ') || 'None'}</dd>
              <dt>Broker account scope</dt>
              <dd>{(user?.brokerAccountIds || []).join(', ') || 'All entitled accounts'}</dd>
            </dl>
          </section>

          <section className="evidence-panel">
            <h2>Diagnostics</h2>
            <dl className="detail-list">
              <dt>Source</dt>
              <dd>{source === 'admin' ? 'Authenticated /admin/health/summary' : source === 'public' ? 'Redacted public summary' : 'Unavailable'}</dd>
              <dt>Status</dt>
              <dd><StatusBadge status={status === 'healthy' ? 'ok' : status === 'blocked' ? 'error' : status === 'degraded' ? 'warning' : 'unknown'} label={status.toUpperCase()} /></dd>
              <dt>Last summary</dt>
              <dd>{formatDateTime(health?.timestamp)}</dd>
            </dl>
          </section>
        </div>

        <section className="evidence-panel">
          <h2>Security Posture</h2>
          <div className="checklist-grid">
            {[
              ['No frontend token persistence', 'Access tokens are held in memory and legacy localStorage sessions are purged.'],
              ['HttpOnly refresh cookie', 'Refresh uses the server-set cookie path when available.'],
              ['Admin diagnostics authenticated', 'Internal diagnostics use /admin/health/summary only after login.'],
              ['Public health redacted', 'Public /readyz and /health/summary remain limited to reachability state.'],
              ['BFF diagnostic bypass blocked', '/bff/health/summary, /bff/readyz, and /bff/dashboard/status are blocked.'],
              ['Dangerous actions gated', 'Kill switch and break-glass actions require admin role, reason, confirmation, audit, and LIVE step-up where enforced.'],
            ].map(([title, body]) => (
              <div className="checklist-item" key={title}>
                <StatusBadge status="ok" />
                <div>
                  <strong>{title}</strong>
                  <p>{body}</p>
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>
    </Gate>
  );
};

export default Settings;
