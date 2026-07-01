import React, { useState, useEffect } from 'react';
import { DefaultService } from '../client';
import { AlertRule, HealthSummary } from '../types/health';

const Alerts: React.FC = () => {
  const [alerts, setAlerts] = useState<AlertRule[]>([]);
  const [healthSummary, setHealthSummary] = useState<HealthSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    const fetchAlerts = async () => {
      try {
        const [response, summary] = await Promise.all([
          DefaultService.getHealthAlerts(),
          DefaultService.getHealthSummary().catch(() => null),
        ]);
        if (active) {
          setAlerts(Array.isArray(response?.alerts) ? response.alerts : []);
          setHealthSummary(summary);
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

  const degradedReasons = healthSummary?.degraded_reasons || [];
  const readinessReason = healthSummary?.readiness?.reason || null;
  const readinessReady = healthSummary?.readiness?.ready ?? healthSummary?.status === 'ok';
  const systemDegraded = Boolean(
    healthSummary
      && (
        healthSummary.status !== 'ok'
        || readinessReady === false
        || degradedReasons.length > 0
      ),
  );
  const firingRules = healthSummary?.alerts?.firing_rules || [];

  return (
    <div>
      <h1>Alerts</h1>
      {loading && <p>Loading...</p>}
      {error && <p>{error}</p>}
      {!loading && !error && systemDegraded && (
        <section className="system-degraded-panel" aria-label="System degradation">
          <div>
            <h2>System Degraded</h2>
            <p>
              {readinessReason || degradedReasons[0] || healthSummary?.status || 'degraded'}
            </p>
          </div>
          {degradedReasons.length > 0 && (
            <ul>
              {degradedReasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          )}
          {firingRules.length === 0 && (
            <p className="system-degraded-panel__note">
              No alert rules are firing; the degradation is coming from readiness checks.
            </p>
          )}
        </section>
      )}
      {alerts.length > 0 && (
        <div className="table-scroll">
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
        </div>
      )}
      {!loading && !error && alerts.length === 0 && <p>No alert rules are firing.</p>}
    </div>
  );
};

export default Alerts;
