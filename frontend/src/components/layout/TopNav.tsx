import React, { useEffect, useMemo, useState } from 'react';
import { useAuth } from '../../auth/AuthContext';
import { AdminService, getTenantId, setTenantId as persistTenantId } from '../../client';
import { ConsoleStatus, statusClass } from '../../lib/consoleUtils';
import './TopNav.css';

interface TenantOption {
  id: string;
  label: string;
}

interface TopNavProps {
  mode?: string | null;
  diagnosticSource?: 'admin' | 'public' | 'unavailable';
  diagnosticStatus?: ConsoleStatus;
}

const TopNav: React.FC<TopNavProps> = ({
  mode,
  diagnosticSource = 'unavailable',
  diagnosticStatus = 'unknown',
}) => {
  const { user, logout } = useAuth();
  const [tenantId, setTenantId] = useState(getTenantId());
  const [tenantOptions, setTenantOptions] = useState<TenantOption[]>([]);

  useEffect(() => {
    let cancelled = false;

    async function loadTenantOptions() {
      if (!user) {
        if (!cancelled) {
          setTenantOptions([]);
        }
        return;
      }

      if (user.canAccessAllTenants) {
        try {
          const response = await AdminService.listTenants();
          if (cancelled) {
            return;
          }
          setTenantOptions(
            (response.tenants || []).map((tenant) => ({
              id: tenant.tenant_id,
              label: tenant.name || tenant.tenant_id,
            })),
          );
          return;
        } catch {
          if (cancelled) {
            return;
          }
        }
      }

      if (!cancelled) {
        setTenantOptions(
          (user.tenantIds || []).map((value) => ({ id: value, label: value })),
        );
      }
    }

    loadTenantOptions();
    return () => {
      cancelled = true;
    };
  }, [user]);

  useEffect(() => {
    if (!tenantOptions.length) {
      if (tenantId) {
        persistTenantId('');
        setTenantId('');
      }
      return;
    }

    const optionIds = new Set(tenantOptions.map((option) => option.id));
    if (tenantId && optionIds.has(tenantId)) {
      return;
    }

    const nextTenantId = tenantOptions[0].id;
    persistTenantId(nextTenantId);
    setTenantId(nextTenantId);
  }, [tenantId, tenantOptions]);

  const sortedTenantOptions = useMemo(
    () => [...tenantOptions].sort((left, right) => left.label.localeCompare(right.label)),
    [tenantOptions],
  );

  const handleTenantApply = () => {
    persistTenantId(tenantId);
    window.location.reload();
  };

  return (
    <div className="top-nav">
      <div className="top-nav__status">
        <div className="logo">Phoenix Operator Console</div>
        <span className={`env-badge env-badge--${String(mode || 'UNKNOWN').toLowerCase()}`}>
          {String(mode || 'UNKNOWN').toUpperCase()}
        </span>
        <span className={statusClass(diagnosticStatus)}>
          {diagnosticSource === 'admin' ? 'Admin diagnostics' : diagnosticSource === 'public' ? 'Public reachability only' : 'Diagnostics unavailable'}
        </span>
      </div>
      <div className="tenant-selector">
        {sortedTenantOptions.length > 0 ? (
          <>
            <select
              aria-label="Tenant"
              value={tenantId}
              onChange={(event) => setTenantId(event.target.value)}
            >
              {sortedTenantOptions.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
            <button type="button" onClick={handleTenantApply}>
              Apply Tenant
            </button>
          </>
        ) : (
          <span className="tenant-hint">No tenant entitlements</span>
        )}
      </div>
      <div className="user-menu">
        <span>{user?.email || 'User'}</span>
        <span className="role-badge">{user?.role || 'viewer'}</span>
        <button type="button" onClick={logout}>
          Logout
        </button>
      </div>
    </div>
  );
};

export default TopNav;
