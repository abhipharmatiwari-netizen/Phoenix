import React, { FormEvent, useCallback, useEffect, useMemo, useState } from 'react';
import { Calendar, Edit2, Key, Plus, RefreshCw, Trash2, X } from 'react-feather';
import {
  AdminService,
  BrokerAccountUpsertPayload,
  SubscriptionUpsertPayload,
  TenantUpsertPayload,
} from '../client';
import Gate from '../auth/Gate';
import { Role } from '../lib/rbac';
import { BrokerAccount, Subscription, Tenant } from '../types/trading';
import ConfirmDialog from '../components/shared/ConfirmDialog';
import './Tenants.css';

type TenantFormState = TenantUpsertPayload;

interface BrokerAccountFormState {
  broker_account_id: string;
  tenant_id: string;
  broker_type: string;
  display_name: string;
  client_code: string;
  secret_ref: string;
  trading_mode: string;
  enabled: boolean;
  default_strategies: string;
}

interface SubscriptionFormState {
  subscription_id: string;
  tenant_id: string;
  broker_account_id: string;
  mode: string;
  start_at: string;
  end_at: string;
}

interface ModalProps {
  title: string;
  children: React.ReactNode;
  onClose: () => void;
}

const EMPTY_TENANT_FORM: TenantFormState = {
  tenant_id: '',
  name: '',
  email: '',
  phone: '',
  status: 'active',
  notes: '',
};

const TERMINAL_VALIDITY_LABEL = 'No active validity';

function Modal({ title, children, onClose }: ModalProps) {
  return (
    <div className="tenant-modal-backdrop" role="presentation">
      <div className="tenant-modal" role="dialog" aria-modal="true" aria-label={title}>
        <div className="tenant-modal__header">
          <h2>{title}</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

function toDateTimeLocal(value?: string | null): string {
  const date = value ? new Date(value) : new Date();
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function toIsoDateTime(value: string): string {
  return new Date(value).toISOString();
}

function addDaysLocal(days: number): string {
  const date = new Date();
  date.setDate(date.getDate() + days);
  return toDateTimeLocal(date.toISOString());
}

function formatDateTime(value?: string | null): string {
  if (!value) {
    return '-';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return '-';
  }
  return date.toLocaleString();
}

function isActiveSubscription(subscription: Subscription): boolean {
  const now = Date.now();
  return new Date(subscription.start_at).getTime() <= now
    && now <= new Date(subscription.end_at).getTime();
}

function isUpcomingSubscription(subscription: Subscription): boolean {
  return new Date(subscription.start_at).getTime() > Date.now();
}

function sortSubscriptionsNewestFirst(items: Subscription[]): Subscription[] {
  return [...items].sort((left, right) =>
    new Date(right.start_at).getTime() - new Date(left.start_at).getTime()
  );
}

function pickDisplaySubscription(items: Subscription[]): Subscription | null {
  const active = sortSubscriptionsNewestFirst(items.filter(isActiveSubscription));
  if (active.length) {
    return active[0];
  }
  const upcoming = sortSubscriptionsNewestFirst(items.filter(isUpcomingSubscription));
  if (upcoming.length) {
    return upcoming[upcoming.length - 1];
  }
  return sortSubscriptionsNewestFirst(items)[0] || null;
}

function subscriptionBadge(subscription: Subscription | null): { label: string; tone: string } {
  if (!subscription) {
    return { label: TERMINAL_VALIDITY_LABEL, tone: 'muted' };
  }
  if (isActiveSubscription(subscription)) {
    return { label: `${subscription.mode} active`, tone: 'ok' };
  }
  if (isUpcomingSubscription(subscription)) {
    return { label: `${subscription.mode} scheduled`, tone: 'warning' };
  }
  return { label: `${subscription.mode} expired`, tone: 'danger' };
}

function tenantValiditySummary(
  tenantId: string,
  accounts: BrokerAccount[],
  subscriptions: Subscription[],
): string {
  const accountIds = new Set(
    accounts
      .filter((account) => account.tenant_id === tenantId)
      .map((account) => account.broker_account_id),
  );
  const tenantSubs = subscriptions.filter((sub) => accountIds.has(sub.broker_account_id));
  const activeSubs = tenantSubs.filter(isActiveSubscription);
  if (activeSubs.length) {
    const earliestExpiry = activeSubs
      .map((sub) => new Date(sub.end_at).getTime())
      .sort((left, right) => left - right)[0];
    return `${activeSubs.length} active, next expiry ${formatDateTime(new Date(earliestExpiry).toISOString())}`;
  }
  const upcomingSubs = tenantSubs.filter(isUpcomingSubscription);
  if (upcomingSubs.length) {
    return `${upcomingSubs.length} scheduled`;
  }
  return TERMINAL_VALIDITY_LABEL;
}

function statusTone(status: string): string {
  const token = status.toLowerCase();
  if (token === 'active') {
    return 'ok';
  }
  if (token === 'suspended') {
    return 'warning';
  }
  return 'muted';
}

function splitStrategies(value: string): string[] {
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

const Tenants: React.FC = () => {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [brokerAccounts, setBrokerAccounts] = useState<BrokerAccount[]>([]);
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([]);
  const [selectedTenantId, setSelectedTenantId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [tenantForm, setTenantForm] = useState<TenantFormState | null>(null);
  const [tenantIdLocked, setTenantIdLocked] = useState(false);
  const [accountForm, setAccountForm] = useState<BrokerAccountFormState | null>(null);
  const [accountIdLocked, setAccountIdLocked] = useState(false);
  const [subscriptionForm, setSubscriptionForm] = useState<SubscriptionFormState | null>(null);
  const [deleteTenant, setDeleteTenant] = useState<Tenant | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [tenantsResponse, brokerAccountsResponse, subscriptionsResponse] = await Promise.all([
        AdminService.listTenants(),
        AdminService.listBrokerAccounts(),
        AdminService.listSubscriptions(),
      ]);
      setTenants(tenantsResponse.tenants || []);
      setBrokerAccounts(brokerAccountsResponse.broker_accounts || []);
      setSubscriptions(subscriptionsResponse.subscriptions || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch tenant data');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  useEffect(() => {
    if (!tenants.length) {
      setSelectedTenantId(null);
      return;
    }
    if (!selectedTenantId || !tenants.some((tenant) => tenant.tenant_id === selectedTenantId)) {
      setSelectedTenantId(tenants[0].tenant_id);
    }
  }, [selectedTenantId, tenants]);

  const selectedTenant = useMemo(
    () => tenants.find((tenant) => tenant.tenant_id === selectedTenantId) || null,
    [selectedTenantId, tenants],
  );

  const selectedAccounts = useMemo(
    () => brokerAccounts.filter((account) => account.tenant_id === selectedTenantId),
    [brokerAccounts, selectedTenantId],
  );

  const subscriptionsByAccount = useMemo(() => {
    const grouped = new Map<string, Subscription[]>();
    subscriptions.forEach((subscription) => {
      const items = grouped.get(subscription.broker_account_id) || [];
      items.push(subscription);
      grouped.set(subscription.broker_account_id, items);
    });
    return grouped;
  }, [subscriptions]);

  const showSuccess = (message: string) => {
    setSuccessMessage(message);
    setTimeout(() => setSuccessMessage(null), 4000);
  };

  const openTenantForm = (tenant?: Tenant) => {
    setTenantIdLocked(Boolean(tenant));
    setTenantForm(tenant ? {
      tenant_id: tenant.tenant_id,
      name: tenant.name || '',
      email: tenant.email || '',
      phone: tenant.phone || '',
      status: tenant.status || 'active',
      notes: tenant.notes || '',
    } : { ...EMPTY_TENANT_FORM });
  };

  const openAccountForm = (account?: BrokerAccount) => {
    setAccountIdLocked(Boolean(account));
    setAccountForm(account ? {
      broker_account_id: account.broker_account_id,
      tenant_id: account.tenant_id,
      broker_type: account.broker_type || 'angel',
      display_name: account.display_name || '',
      client_code: account.client_code || '',
      secret_ref: account.secret_ref || '',
      trading_mode: account.trading_mode || 'PAPER',
      enabled: account.enabled,
      default_strategies: (account.default_strategies || []).join(', '),
    } : {
      broker_account_id: '',
      tenant_id: selectedTenantId || '',
      broker_type: 'angel',
      display_name: '',
      client_code: '',
      secret_ref: '',
      trading_mode: 'PAPER',
      enabled: true,
      default_strategies: '',
    });
  };

  const openSubscriptionForm = (account: BrokerAccount) => {
    const existing = pickDisplaySubscription(subscriptionsByAccount.get(account.broker_account_id) || []);
    const mode = existing?.mode || account.trading_mode || 'PAPER';
    setSubscriptionForm({
      subscription_id: existing?.subscription_id
        || `${account.tenant_id}_${account.broker_account_id}_${mode.toLowerCase()}`,
      tenant_id: account.tenant_id,
      broker_account_id: account.broker_account_id,
      mode,
      start_at: toDateTimeLocal(existing?.start_at),
      end_at: existing ? toDateTimeLocal(existing.end_at) : addDaysLocal(30),
    });
  };

  const handleTenantSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!tenantForm) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await AdminService.upsertTenant({
        ...tenantForm,
        tenant_id: tenantForm.tenant_id.trim(),
        name: tenantForm.name.trim(),
        email: tenantForm.email.trim(),
        phone: tenantForm.phone?.trim() || null,
        notes: tenantForm.notes?.trim() || null,
      });
      setTenantForm(null);
      await loadData();
      setSelectedTenantId(tenantForm.tenant_id.trim());
      showSuccess('Tenant saved');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save tenant');
    } finally {
      setSaving(false);
    }
  };

  const handleAccountSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!accountForm) {
      return;
    }
    const payload: BrokerAccountUpsertPayload = {
      broker_account_id: accountForm.broker_account_id.trim(),
      tenant_id: accountForm.tenant_id.trim(),
      broker_type: accountForm.broker_type.trim(),
      display_name: accountForm.display_name.trim(),
      client_code: accountForm.client_code.trim(),
      secret_ref: accountForm.secret_ref.trim(),
      trading_mode: accountForm.trading_mode,
      enabled: accountForm.enabled,
      default_strategies: splitStrategies(accountForm.default_strategies),
    };
    setSaving(true);
    setError(null);
    try {
      await AdminService.upsertBrokerAccount(payload);
      setAccountForm(null);
      await loadData();
      setSelectedTenantId(payload.tenant_id);
      showSuccess('Broker account saved');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save broker account');
    } finally {
      setSaving(false);
    }
  };

  const handleSubscriptionSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!subscriptionForm) {
      return;
    }
    const payload: SubscriptionUpsertPayload = {
      subscription_id: subscriptionForm.subscription_id.trim(),
      tenant_id: subscriptionForm.tenant_id.trim(),
      broker_account_id: subscriptionForm.broker_account_id.trim(),
      mode: subscriptionForm.mode,
      start_at: toIsoDateTime(subscriptionForm.start_at),
      end_at: toIsoDateTime(subscriptionForm.end_at),
    };
    setSaving(true);
    setError(null);
    try {
      await AdminService.upsertSubscription(payload);
      setSubscriptionForm(null);
      await loadData();
      setSelectedTenantId(payload.tenant_id);
      showSuccess('Validity updated');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update validity');
    } finally {
      setSaving(false);
    }
  };

  const handleDeactivateTenant = async () => {
    if (!deleteTenant) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await AdminService.deactivateTenant(deleteTenant.tenant_id, {
        status: 'archived',
        reason: 'Tenant deleted from admin console',
      });
      setDeleteTenant(null);
      await loadData();
      showSuccess('Tenant archived');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to archive tenant');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Gate requiredRoles={[Role.ADMIN]}>
      <div className="tenants-page">
        <div className="tenants-page__header">
          <div>
            <h1>Tenants</h1>
            <div className="tenant-count">{tenants.length} tenants</div>
          </div>
          <div className="tenant-toolbar">
            <button type="button" className="secondary-button" onClick={loadData} disabled={loading || saving}>
              <RefreshCw size={16} />
              Refresh
            </button>
            <button type="button" className="primary-button" onClick={() => openTenantForm()} disabled={saving}>
              <Plus size={16} />
              Add Tenant
            </button>
          </div>
        </div>

        {error && <div className="tenant-alert tenant-alert--error">{error}</div>}
        {successMessage && <div className="tenant-alert tenant-alert--success">{successMessage}</div>}

        <div className="tenants-layout">
          <section className="tenant-list-panel">
            {loading ? (
              <div className="tenant-empty">Loading...</div>
            ) : tenants.length === 0 ? (
              <div className="tenant-empty">No tenants found.</div>
            ) : (
              <div className="tenant-table-wrap">
                <table className="tenant-table">
                  <thead>
                    <tr>
                      <th>Tenant</th>
                      <th>Status</th>
                      <th>Accounts</th>
                      <th>Validity</th>
                      <th aria-label="Actions" />
                    </tr>
                  </thead>
                  <tbody>
                    {tenants.map((tenant) => {
                      const tenantAccounts = brokerAccounts.filter(
                        (account) => account.tenant_id === tenant.tenant_id,
                      );
                      return (
                        <tr
                          key={tenant.tenant_id}
                          className={tenant.tenant_id === selectedTenantId ? 'is-selected' : ''}
                          onClick={() => setSelectedTenantId(tenant.tenant_id)}
                        >
                          <td>
                            <div className="tenant-name">{tenant.name || tenant.tenant_id}</div>
                            <div className="tenant-subtext">{tenant.email || tenant.tenant_id}</div>
                          </td>
                          <td>
                            <span className={`tenant-pill tenant-pill--${statusTone(tenant.status || '')}`}>
                              {(tenant.status || 'unknown').toUpperCase()}
                            </span>
                          </td>
                          <td>{tenantAccounts.length}</td>
                          <td>{tenantValiditySummary(tenant.tenant_id, brokerAccounts, subscriptions)}</td>
                          <td>
                            <div className="row-actions" onClick={(event) => event.stopPropagation()}>
                              <button type="button" className="icon-button" onClick={() => openTenantForm(tenant)} aria-label="Edit tenant">
                                <Edit2 size={16} />
                              </button>
                              <button type="button" className="icon-button icon-button--danger" onClick={() => setDeleteTenant(tenant)} aria-label="Delete tenant">
                                <Trash2 size={16} />
                              </button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="tenant-detail-panel">
            {selectedTenant ? (
              <>
                <div className="tenant-detail-panel__header">
                  <div>
                    <h2>{selectedTenant.name || selectedTenant.tenant_id}</h2>
                    <div className="tenant-subtext">{selectedTenant.tenant_id}</div>
                  </div>
                  <button type="button" className="secondary-button" onClick={() => openAccountForm()} disabled={saving}>
                    <Key size={16} />
                    Add Account
                  </button>
                </div>

                <div className="tenant-fields">
                  <div>
                    <span>Email</span>
                    <strong>{selectedTenant.email || '-'}</strong>
                  </div>
                  <div>
                    <span>Phone</span>
                    <strong>{selectedTenant.phone || '-'}</strong>
                  </div>
                  <div>
                    <span>Status</span>
                    <strong>{(selectedTenant.status || 'unknown').toUpperCase()}</strong>
                  </div>
                  <div>
                    <span>Updated</span>
                    <strong>{formatDateTime(selectedTenant.updated_at)}</strong>
                  </div>
                </div>

                <div className="accounts-header">
                  <h3>Broker Accounts</h3>
                  <span>{selectedAccounts.length}</span>
                </div>

                {selectedAccounts.length === 0 ? (
                  <div className="tenant-empty tenant-empty--compact">No broker accounts.</div>
                ) : (
                  <div className="account-list">
                    {selectedAccounts.map((account) => {
                      const displaySubscription = pickDisplaySubscription(
                        subscriptionsByAccount.get(account.broker_account_id) || [],
                      );
                      const badge = subscriptionBadge(displaySubscription);
                      return (
                        <div className="account-row" key={account.broker_account_id}>
                          <div className="account-row__main">
                            <div>
                              <div className="account-name">{account.display_name || account.broker_account_id}</div>
                              <div className="tenant-subtext">
                                {account.broker_account_id} / {account.client_code || '-'}
                              </div>
                            </div>
                            <div className="account-meta">
                              <span className={`tenant-pill tenant-pill--${account.enabled ? 'ok' : 'muted'}`}>
                                {account.enabled ? 'ENABLED' : 'DISABLED'}
                              </span>
                              <span className={`tenant-pill tenant-pill--${badge.tone}`}>
                                {badge.label}
                              </span>
                            </div>
                          </div>
                          <div className="validity-row">
                            <span>Start {formatDateTime(displaySubscription?.start_at)}</span>
                            <span>End {formatDateTime(displaySubscription?.end_at)}</span>
                          </div>
                          <div className="account-actions">
                            <button type="button" className="secondary-button" onClick={() => openAccountForm(account)} disabled={saving}>
                              <Edit2 size={15} />
                              Edit
                            </button>
                            <button type="button" className="secondary-button" onClick={() => openSubscriptionForm(account)} disabled={saving}>
                              <Calendar size={15} />
                              Validity
                            </button>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </>
            ) : (
              <div className="tenant-empty">No tenant selected.</div>
            )}
          </section>
        </div>

        {tenantForm && (
          <Modal title={tenantIdLocked ? 'Edit Tenant' : 'Add Tenant'} onClose={() => setTenantForm(null)}>
            <form className="tenant-form" onSubmit={handleTenantSubmit}>
              <label>
                Tenant ID
                <input
                  required
                  value={tenantForm.tenant_id}
                  disabled={tenantIdLocked}
                  onChange={(event) => setTenantForm({ ...tenantForm, tenant_id: event.target.value })}
                />
              </label>
              <label>
                Name
                <input required value={tenantForm.name} onChange={(event) => setTenantForm({ ...tenantForm, name: event.target.value })} />
              </label>
              <label>
                Email
                <input required type="email" value={tenantForm.email} onChange={(event) => setTenantForm({ ...tenantForm, email: event.target.value })} />
              </label>
              <label>
                Phone
                <input value={tenantForm.phone || ''} onChange={(event) => setTenantForm({ ...tenantForm, phone: event.target.value })} />
              </label>
              <label>
                Status
                <select value={tenantForm.status} onChange={(event) => setTenantForm({ ...tenantForm, status: event.target.value })}>
                  <option value="active">Active</option>
                  <option value="suspended">Suspended</option>
                  <option value="archived">Archived</option>
                </select>
              </label>
              <label className="tenant-form__wide">
                Notes
                <textarea value={tenantForm.notes || ''} onChange={(event) => setTenantForm({ ...tenantForm, notes: event.target.value })} />
              </label>
              <div className="tenant-form__actions">
                <button type="button" className="secondary-button" onClick={() => setTenantForm(null)}>Cancel</button>
                <button type="submit" className="primary-button" disabled={saving}>Save</button>
              </div>
            </form>
          </Modal>
        )}

        {accountForm && (
          <Modal title={accountIdLocked ? 'Edit Broker Account' : 'Add Broker Account'} onClose={() => setAccountForm(null)}>
            <form className="tenant-form" onSubmit={handleAccountSubmit}>
              <label>
                Account ID
                <input required disabled={accountIdLocked} value={accountForm.broker_account_id} onChange={(event) => setAccountForm({ ...accountForm, broker_account_id: event.target.value })} />
              </label>
              <label>
                Tenant
                <select required value={accountForm.tenant_id} onChange={(event) => setAccountForm({ ...accountForm, tenant_id: event.target.value })}>
                  {tenants.map((tenant) => (
                    <option key={tenant.tenant_id} value={tenant.tenant_id}>{tenant.name || tenant.tenant_id}</option>
                  ))}
                </select>
              </label>
              <label>
                Broker
                <input required value={accountForm.broker_type} onChange={(event) => setAccountForm({ ...accountForm, broker_type: event.target.value })} />
              </label>
              <label>
                Display Name
                <input required value={accountForm.display_name} onChange={(event) => setAccountForm({ ...accountForm, display_name: event.target.value })} />
              </label>
              <label>
                Client Code
                <input required value={accountForm.client_code} onChange={(event) => setAccountForm({ ...accountForm, client_code: event.target.value })} />
              </label>
              <label>
                Secret Ref
                <input required value={accountForm.secret_ref} onChange={(event) => setAccountForm({ ...accountForm, secret_ref: event.target.value })} />
              </label>
              <label>
                Mode
                <select value={accountForm.trading_mode} onChange={(event) => setAccountForm({ ...accountForm, trading_mode: event.target.value })}>
                  <option value="PAPER">PAPER</option>
                  <option value="SHADOW">SHADOW</option>
                  <option value="LIVE">LIVE</option>
                </select>
              </label>
              <label className="checkbox-field">
                <input type="checkbox" checked={accountForm.enabled} onChange={(event) => setAccountForm({ ...accountForm, enabled: event.target.checked })} />
                Enabled
              </label>
              <label className="tenant-form__wide">
                Default Strategies
                <input value={accountForm.default_strategies} onChange={(event) => setAccountForm({ ...accountForm, default_strategies: event.target.value })} />
              </label>
              <div className="tenant-form__actions">
                <button type="button" className="secondary-button" onClick={() => setAccountForm(null)}>Cancel</button>
                <button type="submit" className="primary-button" disabled={saving}>Save</button>
              </div>
            </form>
          </Modal>
        )}

        {subscriptionForm && (
          <Modal title="Assign Validity" onClose={() => setSubscriptionForm(null)}>
            <form className="tenant-form" onSubmit={handleSubscriptionSubmit}>
              <label>
                Subscription ID
                <input required value={subscriptionForm.subscription_id} onChange={(event) => setSubscriptionForm({ ...subscriptionForm, subscription_id: event.target.value })} />
              </label>
              <label>
                Mode
                <select value={subscriptionForm.mode} onChange={(event) => setSubscriptionForm({ ...subscriptionForm, mode: event.target.value })}>
                  <option value="PAPER">PAPER</option>
                  <option value="SHADOW">SHADOW</option>
                  <option value="LIVE">LIVE</option>
                </select>
              </label>
              <label>
                Start
                <input required type="datetime-local" value={subscriptionForm.start_at} onChange={(event) => setSubscriptionForm({ ...subscriptionForm, start_at: event.target.value })} />
              </label>
              <label>
                End
                <input required type="datetime-local" value={subscriptionForm.end_at} onChange={(event) => setSubscriptionForm({ ...subscriptionForm, end_at: event.target.value })} />
              </label>
              <div className="tenant-form__actions">
                <button type="button" className="secondary-button" onClick={() => setSubscriptionForm(null)}>Cancel</button>
                <button type="submit" className="primary-button" disabled={saving}>Save</button>
              </div>
            </form>
          </Modal>
        )}

        {deleteTenant && (
          <ConfirmDialog
            title="Delete Tenant"
            message={`Archive ${deleteTenant.name || deleteTenant.tenant_id}, disable its broker accounts, and expire its validity windows.`}
            confirmText="Delete"
            requireTyped={deleteTenant.name || deleteTenant.tenant_id}
            onCancel={() => setDeleteTenant(null)}
            onConfirm={handleDeactivateTenant}
          />
        )}
      </div>
    </Gate>
  );
};

export default Tenants;
