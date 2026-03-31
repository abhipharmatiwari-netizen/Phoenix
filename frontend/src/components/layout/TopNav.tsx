import React, { useState } from 'react';
import { useAuth } from '../../auth/AuthContext';
import { getTenantId, setTenantId as persistTenantId } from '../../client';
import './TopNav.css';

const TopNav: React.FC = () => {
  const { user, logout } = useAuth();
  const [tenantId, setTenantId] = useState(getTenantId());

  const handleTenantApply = () => {
    persistTenantId(tenantId);
    window.location.reload();
  };

  return (
    <div className="top-nav">
      <div className="logo">Phoenix v9</div>
      <div className="tenant-selector">
        <input
          aria-label="Tenant ID"
          value={tenantId}
          onChange={(event) => setTenantId(event.target.value)}
          placeholder="tenant-1"
        />
        <button type="button" onClick={handleTenantApply}>
          Apply Tenant
        </button>
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
