import React, { useState, useEffect } from 'react';
import { DefaultService } from '../client';
import HealthTile from '../components/health/HealthTile';
import { HealthSummary } from '../types/health';

const Overview: React.FC = () => {
  const [healthSummary, setHealthSummary] = useState<HealthSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    const fetchHealthSummary = async () => {
      try {
        const summary = await DefaultService.getHealthSummary();
        if (active) {
          setHealthSummary(summary);
        }
      } catch (err) {
        if (active) {
          setError(err instanceof Error ? err.message : 'Failed to fetch health summary');
        }
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    };

    fetchHealthSummary();

    return () => {
      active = false;
    };
  }, []);

  const getStatusColor = (status: string): 'green' | 'yellow' | 'red' => {
    switch (status.toLowerCase()) {
      case 'ok':
        return 'green';
      case 'running':
        return 'green';
      case 'degraded':
        return 'yellow';
      default:
        return 'red';
    }
  };

  return (
    <div>
      <h1>Overview</h1>
      {loading && <p>Loading...</p>}
      {error && <p>{error}</p>}
      {healthSummary && (
        <div className="health-tiles">
          <HealthTile
            title="Overall Status"
            status={getStatusColor(healthSummary.status)}
            value={healthSummary.status}
          />
          <HealthTile
            title="Stream Worker"
            status={
              healthSummary.stream_worker_running
                ? 'green'
                : healthSummary.stream_worker_expected === false
                  ? 'green'
                  : 'red'
            }
            value={
              healthSummary.stream_worker_running
                ? 'Running'
                : healthSummary.stream_worker_expected === false
                  ? 'N/A (Hub Mode)'
                  : 'Stopped'
            }
          />
          <HealthTile
            title="Watchdog"
            status={
              healthSummary.watchdog_running
                ? 'green'
                : healthSummary.stream_worker_expected === false
                  ? 'green'
                  : 'red'
            }
            value={
              healthSummary.watchdog_running
                ? 'Running'
                : healthSummary.stream_worker_expected === false
                  ? 'N/A (Hub Mode)'
                  : 'Stopped'
            }
          />
          <HealthTile
            title="Schema Status"
            status={getStatusColor(healthSummary.schema_status)}
            value={healthSummary.schema_status}
          />
          <HealthTile
            title="Tracked Accounts"
            status={healthSummary.tracked_account_count > 0 ? 'green' : 'yellow'}
            value={String(healthSummary.tracked_account_count)}
          />
          <HealthTile
            title="Active Alerts"
            status={healthSummary.alerts.firing_count > 0 ? 'yellow' : 'green'}
            value={String(healthSummary.alerts.firing_count)}
          />
        </div>
      )}
      {healthSummary && healthSummary.degraded_reasons.length > 0 && (
        <div style={{ marginTop: '1rem' }}>
          <h2>Degraded Reasons</h2>
          <ul>
            {healthSummary.degraded_reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
};

export default Overview;

