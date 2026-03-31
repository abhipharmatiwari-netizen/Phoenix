import React, { useState, useEffect } from 'react';
import { AdminService } from '../client';
import Gate from '../auth/Gate';
import { Role } from '../lib/rbac';
import { BrokerAccount, Tenant } from '../types/trading';

const Tenants: React.FC = () => {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [brokerAccounts, setBrokerAccounts] = useState<BrokerAccount[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    const fetchData = async () => {
      try {
        const tenantsResponse = await AdminService.listTenants();
        const brokerAccountsResponse = await AdminService.listBrokerAccounts();
        if (active) {
          setTenants(tenantsResponse.tenants);
          setBrokerAccounts(brokerAccountsResponse.broker_accounts);
        }
      } catch (err) {
        if (active) {
          setError(err instanceof Error ? err.message : 'Failed to fetch data');
        }
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    };

    fetchData();

    return () => {
      active = false;
    };
  }, []);

  return (
    <Gate requiredRoles={[Role.ADMIN]}>
      <div>
        <h1>Tenants</h1>
        {loading && <p>Loading...</p>}
        {error && <p>{error}</p>}
        {!loading && !error && tenants.length === 0 && <p>No tenants found.</p>}
        {tenants.map((tenant) => (
          <div key={tenant.tenant_id}>
            <h2>{tenant.name}</h2>
            <ul>
              {brokerAccounts
                .filter((account) => account.tenant_id === tenant.tenant_id)
                .map((account) => (
                  <li key={account.broker_account_id}>
                    {account.display_name} ({account.trading_mode})
                  </li>
                ))}
            </ul>
          </div>
        ))}
      </div>
    </Gate>
  );
};

export default Tenants;
