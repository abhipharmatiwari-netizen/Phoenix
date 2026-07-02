import React, { useCallback, useEffect, useMemo, useState } from 'react';
import Gate from '../auth/Gate';
import { AdminService, DefaultService } from '../client';
import LoadingSpinner from '../components/shared/LoadingSpinner';
import StatusBadge from '../components/shared/StatusBadge';
import { Role } from '../lib/rbac';
import { BrokerAccount, Subscription, Tenant } from '../types/trading';
import { AccountStaleness } from '../types/health';
import {
  formatAge,
  formatDateTime,
  normalizeStatus,
} from '../lib/consoleUtils';

interface AccountRow extends Record<string, unknown> {
  tenant: string;
  tenant_id: string;
  account: string;
  broker_account_id: string;
  mode: string;
  enabled: boolean;
  subscription: string;
  credential_status: string;
  sync_status: string;
  sync_age: string;
  broker_login_health: string;
  margin: string;
}

function activeSubscriptionFor(account: BrokerAccount, subscriptions: Subscription[]): Subscription | null {
  const now = Date.now();
  return subscriptions
    .filter((sub) => sub.broker_account_id === account.broker_account_id)
    .sort((left, right) => new Date(right.start_at).getTime() - new Date(left.start_at).getTime())
    .find((sub) => new Date(sub.start_at).getTime() <= now && now <= new Date(sub.end_at).getTime())
    || null;
}

const Accounts: React.FC = () => {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [accounts, setAccounts] = useState<BrokerAccount[]>([]);
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([]);
  const [staleness, setStaleness] = useState<AccountStaleness[]>([]);
  const [healthSource, setHealthSource] = useState<'admin' | 'public' | 'unavailable'>('unavailable');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [tenantsResp, accountsResp, subsResp, healthResp] = await Promise.all([
        AdminService.listTenants(),
        AdminService.listBrokerAccounts(),
        AdminService.listSubscriptions(),
        DefaultService.getOperatorHealthSummary().catch(() => null),
      ]);
      setTenants(tenantsResp.tenants || []);
      setAccounts(accountsResp.broker_accounts || []);
      setSubscriptions(subsResp.subscriptions || []);
      setStaleness(healthResp?.summary.per_account_staleness || []);
      setHealthSource(healthResp?.source || 'unavailable');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load accounts');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const tenantNames = useMemo(() => new Map(tenants.map((tenant) => [
    tenant.tenant_id,
    tenant.name || tenant.tenant_id,
  ])), [tenants]);

  const stalenessByAccount = useMemo(() => new Map(staleness.map((item) => [
    item.broker_account_id,
    item,
  ])), [staleness]);

  const rows = useMemo<AccountRow[]>(() => accounts.map((account) => {
    const activeSub = activeSubscriptionFor(account, subscriptions);
    const stale = stalenessByAccount.get(account.broker_account_id);
    const syncStatus = stale
      ? stale.stale ? 'blocked' : 'healthy'
      : healthSource === 'admin' ? 'unknown' : 'unknown';
    return {
      tenant: tenantNames.get(account.tenant_id) || account.tenant_id,
      tenant_id: account.tenant_id,
      account: account.display_name || account.broker_account_id,
      broker_account_id: account.broker_account_id,
      mode: account.trading_mode || 'UNKNOWN',
      enabled: Boolean(account.enabled),
      subscription: activeSub
        ? `${activeSub.mode} until ${formatDateTime(activeSub.end_at)}`
        : 'No active subscription',
      credential_status: account.secret_ref ? 'Configured' : 'Missing',
      sync_status: syncStatus,
      sync_age: stale?.last_sync_ts ? formatAge(stale.last_sync_ts) : 'Unknown',
      broker_login_health: stale?.error_reason ? `Blocked: ${stale.error_reason}` : 'Unknown',
      margin: 'Unavailable',
    };
  }), [accounts, healthSource, stalenessByAccount, subscriptions, tenantNames]);

  return (
    <Gate requiredRoles={[Role.READONLY]}>
      <div className="console-page">
        <div className="page-header">
          <div>
            <h1>Accounts</h1>
            <p>Tenants, broker accounts, subscriptions, credential status, and sync freshness.</p>
          </div>
          <button className="secondary-button" type="button" onClick={load} disabled={loading}>
            Refresh
          </button>
        </div>

        {error && <div className="notice notice--blocked">{error}</div>}
        {healthSource !== 'admin' && (
          <div className="notice notice--warning">
            Authenticated operator health is unavailable. Sync and broker-login fields are not trusted.
          </div>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : (
          <div className="responsive-table" data-testid="accounts-table">
            <table>
              <thead>
                <tr>
                  <th>Tenant</th>
                  <th>Account</th>
                  <th>Mode</th>
                  <th>Enabled</th>
                  <th>Subscription</th>
                  <th>Credential</th>
                  <th>Broker Sync</th>
                  <th>Balance / Margin</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.broker_account_id}>
                    <td data-label="Tenant">
                      <strong>{row.tenant}</strong>
                      <span>{row.tenant_id}</span>
                    </td>
                    <td data-label="Account">
                      <strong>{row.account}</strong>
                      <span>{row.broker_account_id}</span>
                    </td>
                    <td data-label="Mode">
                      <span className={`env-badge env-badge--${row.mode.toLowerCase()}`}>{row.mode}</span>
                    </td>
                    <td data-label="Enabled">
                      <StatusBadge status={row.enabled ? 'ok' : 'unknown'} label={row.enabled ? 'Enabled' : 'Disabled'} />
                    </td>
                    <td data-label="Subscription">{row.subscription}</td>
                    <td data-label="Credential">
                      <StatusBadge
                        status={row.credential_status === 'Configured' ? 'ok' : 'error'}
                        label={row.credential_status}
                      />
                    </td>
                    <td data-label="Broker Sync">
                      <StatusBadge
                        status={normalizeStatus(row.sync_status) === 'healthy' ? 'ok' : normalizeStatus(row.sync_status) === 'blocked' ? 'error' : 'unknown'}
                        label={row.sync_age}
                      />
                    </td>
                    <td data-label="Balance / Margin">{row.margin}</td>
                  </tr>
                ))}
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={8}>No accounts are visible to this admin session.</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Gate>
  );
};

export default Accounts;
