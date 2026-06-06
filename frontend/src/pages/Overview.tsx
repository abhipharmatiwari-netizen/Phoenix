import React, { useState, useEffect } from 'react';
import { DefaultService } from '../client';
import HealthTile from '../components/health/HealthTile';
import { HealthSummary } from '../types/health';

type PublicHealthSummary = HealthSummary & { ready?: boolean };

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
    const timer = window.setInterval(fetchHealthSummary, 30_000);

    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  const getStatusColor = (status: string | null | undefined): 'green' | 'yellow' | 'red' => {
    switch (String(status || 'unknown').toLowerCase()) {
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

  const publicHealth = healthSummary as PublicHealthSummary | null;
  const overallReady = healthSummary?.readiness?.ready ?? publicHealth?.ready ?? healthSummary?.status === 'ok';
  const overallValue = overallReady
    ? (healthSummary?.status || 'ok')
    : (healthSummary?.readiness?.reason || healthSummary?.status || 'degraded');
  const schemaStatus = healthSummary?.schema_status || healthSummary?.schema?.status || 'unknown';
  const streamWorkerRunning = healthSummary?.stream_worker_running;
  const streamWorkerExpected = healthSummary?.stream_worker_expected;
  const watchdogRunning = healthSummary?.watchdog_running;
  const trackedAccountCount = healthSummary?.tracked_account_count;
  const firingAlertCount = healthSummary?.alerts?.firing_count ?? 0;
  const degradedReasons = healthSummary?.degraded_reasons || [];

  const getRuntimeTile = (running: boolean | undefined, expected: boolean | undefined) => {
    if (running === true) {
      return { status: 'green' as const, value: 'Running' };
    }
    if (expected === false) {
      return { status: 'green' as const, value: 'N/A (Hub Mode)' };
    }
    if (running === false) {
      return { status: 'red' as const, value: 'Stopped' };
    }
    return { status: 'yellow' as const, value: 'Unknown' };
  };

  const streamWorkerTile = getRuntimeTile(streamWorkerRunning, streamWorkerExpected);
  const watchdogTile = getRuntimeTile(watchdogRunning, streamWorkerExpected);

  return (
    <div>
      <h1>Overview</h1>
      {loading && <p>Loading...</p>}
      {error && <p>{error}</p>}
      {healthSummary && (
        <div className="health-tiles">
          <HealthTile
            title="Overall Status"
            status={overallReady && healthSummary.status === 'ok' ? 'green' : 'red'}
            value={overallValue}
          />
          <HealthTile
            title="Stream Worker"
            status={streamWorkerTile.status}
            value={streamWorkerTile.value}
          />
          <HealthTile
            title="Watchdog"
            status={watchdogTile.status}
            value={watchdogTile.value}
          />
          <HealthTile
            title="Schema Status"
            status={getStatusColor(schemaStatus)}
            value={schemaStatus}
          />
          <HealthTile
            title="Tracked Accounts"
            status={trackedAccountCount === undefined ? 'yellow' : trackedAccountCount > 0 ? 'green' : 'yellow'}
            value={trackedAccountCount === undefined ? 'Unknown' : String(trackedAccountCount)}
          />
          <HealthTile
            title="Active Alerts"
            status={firingAlertCount > 0 ? 'yellow' : 'green'}
            value={String(firingAlertCount)}
          />
        </div>
      )}
      {healthSummary && degradedReasons.length > 0 && (
        <div style={{ marginTop: '1rem' }}>
          <h2>Degraded Reasons</h2>
          <ul>
            {degradedReasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
};

export default Overview;
