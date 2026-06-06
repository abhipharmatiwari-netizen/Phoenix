import React, { useState, useEffect } from 'react';
import { DefaultService } from '../client';
import { MitigationsResponse } from '../types/health';

const Mitigations: React.FC = () => {
  const [mitigations, setMitigations] = useState<MitigationsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    const fetchMitigations = async () => {
      try {
        const response = await DefaultService.getHealthMitigations();
        if (active) {
          setMitigations({
            enabled: Boolean(response?.enabled),
            rule_count: Number(response?.rule_count ?? 0),
            total_events: Number(response?.total_events ?? 0),
            recent_events: Array.isArray(response?.recent_events) ? response.recent_events : [],
            fault_counts: response?.fault_counts && typeof response.fault_counts === 'object'
              ? response.fault_counts
              : {},
          });
        }
      } catch (err) {
        if (active) {
          setError(err instanceof Error ? err.message : 'Failed to fetch mitigations');
        }
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    };

    fetchMitigations();

    return () => {
      active = false;
    };
  }, []);

  return (
    <div>
      <h1>Mitigations</h1>
      {loading && <p>Loading...</p>}
      {error && <p>{error}</p>}
      {mitigations && (
        <div>
          <p>Enabled: {mitigations.enabled ? 'Yes' : 'No'}</p>
          <p>Triggered Events: {mitigations.total_events}</p>
          <h2>Recent Events</h2>
          {mitigations.recent_events.length === 0 ? (
            <p>No mitigation events have been recorded.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Rule</th>
                  <th>Action</th>
                  <th>Scope</th>
                  <th>Fault Count</th>
                </tr>
              </thead>
              <tbody>
                {mitigations.recent_events.map((event) => (
                  <tr key={`${event.rule_name}-${event.timestamp}`}>
                    <td>{event.rule_name}</td>
                    <td>{event.action}</td>
                    <td>{event.scope_key ? `${event.scope_key}:${event.scope_value}` : 'global'}</td>
                    <td>{event.fault_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <h2>Fault Counts</h2>
          <pre>{JSON.stringify(mitigations.fault_counts, null, 2)}</pre>
        </div>
      )}
    </div>
  );
};

export default Mitigations;
