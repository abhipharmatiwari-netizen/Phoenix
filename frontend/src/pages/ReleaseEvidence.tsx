import React, { useCallback, useEffect, useMemo, useState } from 'react';
import Gate from '../auth/Gate';
import { AdminService } from '../client';
import LoadingSpinner from '../components/shared/LoadingSpinner';
import StatusBadge from '../components/shared/StatusBadge';
import { coerceRecord, compactJson, displayValue, formatDateTime, normalizeStatus } from '../lib/consoleUtils';
import { Role } from '../lib/rbac';

function firstValue(record: Record<string, unknown>, keys: string[]): unknown {
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== null && record[key] !== '') {
      return record[key];
    }
  }
  return undefined;
}

const ReleaseEvidence: React.FC = () => {
  const [evidence, setEvidence] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setEvidence(await AdminService.getReleaseEvidence());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load release evidence');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const cards = useMemo(() => {
    const record = evidence || {};
    const readiness = coerceRecord(firstValue(record, ['readiness', 'readyz', 'readiness_evidence']));
    const validation = firstValue(record, ['validation_status', 'status', 'validation']);
    return [
      { label: 'Build SHA', value: displayValue(firstValue(record, ['build_sha', 'git_sha', 'commit_sha', 'sha'])) },
      { label: 'Image Tag', value: displayValue(firstValue(record, ['image_tag', 'container_image', 'docker_image'])) },
      { label: 'Runtime Mode', value: displayValue(firstValue(record, ['trade_mode', 'runtime_mode', 'operating_mode'])) },
      { label: 'Migration State', value: displayValue(firstValue(record, ['migration_state', 'schema_status', 'migrations'])) },
      { label: 'Readiness', value: displayValue(firstValue(readiness, ['reason', 'status', 'ready'])), status: normalizeStatus(firstValue(readiness, ['status', 'ready'])) },
      { label: 'Operator Owners', value: displayValue(firstValue(record, ['operator_owners', 'owners', 'operators'])) },
      { label: 'Deployment Time', value: formatDateTime(firstValue(record, ['deployment_timestamp', 'deployed_at', 'timestamp'])) },
      { label: 'Validation', value: displayValue(validation), status: normalizeStatus(validation) },
    ];
  }, [evidence]);

  return (
    <Gate requiredRoles={[Role.OPERATOR]}>
      <div className="console-page">
        <div className="page-header">
          <div>
            <h1>Release Evidence</h1>
            <p>Authenticated deployment, migration, readiness, owner, and validation evidence.</p>
          </div>
          <button className="secondary-button" type="button" onClick={load} disabled={loading}>
            Refresh
          </button>
        </div>

        {error && <div className="notice notice--blocked">{error}</div>}
        {loading ? (
          <LoadingSpinner />
        ) : evidence ? (
          <>
            <div className="metric-grid">
              {cards.map((card) => (
                <section className="metric-card" key={card.label}>
                  <span>{card.label}</span>
                  <strong>{card.value}</strong>
                  {card.status && <StatusBadge status={card.status === 'healthy' ? 'ok' : card.status === 'blocked' ? 'error' : card.status === 'degraded' ? 'warning' : 'unknown'} />}
                </section>
              ))}
            </div>
            <section className="evidence-panel">
              <h2>Full Evidence</h2>
              <pre className="evidence-block">{compactJson(evidence)}</pre>
            </section>
          </>
        ) : (
          <div className="notice notice--warning">Release evidence is unavailable.</div>
        )}
      </div>
    </Gate>
  );
};

export default ReleaseEvidence;
