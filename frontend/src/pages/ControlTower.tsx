import React, { useState, useEffect } from 'react';
import { ControlTowerService } from '../client';
import Gate from '../auth/Gate';
import { Role } from '../lib/rbac';
import { MatrixResponse } from '../types/controlTower';
import './ControlTower.css';

const ControlTower: React.FC = () => {
  const [matrix, setMatrix] = useState<MatrixResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    const fetchMatrix = async () => {
      try {
        const response = await ControlTowerService.getControlTowerMatrix();
        if (active) {
          setMatrix(response);
        }
      } catch (err) {
        if (active) {
          setError(err instanceof Error ? err.message : 'Failed to fetch control tower matrix');
        }
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    };

    fetchMatrix();

    return () => {
      active = false;
    };
  }, []);

  const handleToggle = async (tenantId: string, strategyId: string, enabled: boolean) => {
    setError(null);
    setSuccessMessage(null);
    try {
      setPendingKey(`${tenantId}:${strategyId}`);
      await ControlTowerService.toggleControlTower({ tenant_id: tenantId, strategy_id: strategyId, enabled });
      const response = await ControlTowerService.getControlTowerMatrix();
      setMatrix(response);
      const strategyLabel = strategyId.replace(/_/g, ' ').replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
      const tenantLabel = response.tenants.find(t => t.tenant_id === tenantId)?.name || tenantId;
      setSuccessMessage(`${strategyLabel} ${enabled ? 'enabled' : 'disabled'} for ${tenantLabel}`);
      setTimeout(() => setSuccessMessage(null), 4000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to toggle strategy');
    } finally {
      setPendingKey(null);
    }
  };

  const formatStrategyName = (name: string): string =>
    name.replace(/[-_]/g, ' ').replace(/\b\w/g, c => c.toUpperCase());

  return (
    <Gate requiredRoles={[Role.OPERATOR, Role.ADMIN]}>
      <div className="control-tower-container">
        <h1 className="control-tower-title">Control Tower</h1>
        <p className="control-tower-subtitle">Enable or disable strategies per tenant</p>
        {loading && <p className="loading-message">Loading...</p>}
        {error && <p className="error-message">{error}</p>}
        {successMessage && <p className="success-message">{successMessage}</p>}
        {matrix && (
          <table className="control-tower-table">
            <thead>
              <tr>
                <th>Strategy</th>
                {matrix.tenants.map((tenant) => (
                  <th key={tenant.tenant_id}>
                    {tenant.name || tenant.tenant_id}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {matrix.strategies.map((strategy) => (
                <tr key={strategy.strategy_id}>
                  <td>{formatStrategyName(strategy.display_name)}</td>
                  {matrix.tenants.map((tenant) => {
                    const toggleKey = `${tenant.tenant_id}:${strategy.strategy_id}`;
                    return (
                      <td key={tenant.tenant_id}>
                        <input
                          type="checkbox"
                          checked={matrix.matrix[tenant.tenant_id]?.[strategy.strategy_id] || false}
                          disabled={pendingKey === toggleKey}
                          onChange={(event) =>
                            handleToggle(tenant.tenant_id, strategy.strategy_id, event.target.checked)
                          }
                        />
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </Gate>
  );
};

export default ControlTower;
