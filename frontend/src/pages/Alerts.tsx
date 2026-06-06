import React, { useState, useEffect } from 'react';
import { DefaultService } from '../client';
import { AlertRule } from '../types/health';

const Alerts: React.FC = () => {
  const [alerts, setAlerts] = useState<AlertRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    const fetchAlerts = async () => {
      try {
        const response = await DefaultService.getHealthAlerts();
        if (active) {
          setAlerts(Array.isArray(response?.alerts) ? response.alerts : []);
        }
      } catch (err) {
        if (active) {
          setError(err instanceof Error ? err.message : 'Failed to fetch alerts');
        }
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    };

    fetchAlerts();

    return () => {
      active = false;
    };
  }, []);

  return (
    <div>
      <h1>Alerts</h1>
      {loading && <p>Loading...</p>}
      {error && <p>{error}</p>}
      {alerts.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Rule</th>
              <th>Severity</th>
              <th>State</th>
              <th>Message</th>
              <th>Value</th>
            </tr>
          </thead>
          <tbody>
            {alerts.map((alert, index) => (
              <tr key={index}>
                <td>{alert.rule_name}</td>
                <td>{alert.severity}</td>
                <td>{alert.state}</td>
                <td>{alert.message}</td>
                <td>{alert.value ?? '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {!loading && !error && alerts.length === 0 && <p>No alert rules are firing.</p>}
    </div>
  );
};

export default Alerts;
