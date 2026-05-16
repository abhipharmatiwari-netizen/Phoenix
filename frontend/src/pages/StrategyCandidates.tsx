import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { CheckCircle, RefreshCw, XCircle } from 'react-feather';
import Gate from '../auth/Gate';
import {
  StrategyCandidate,
  StrategyCandidateDiff,
  StrategyCandidateService,
} from '../client';
import { Role } from '../lib/rbac';
import './StrategyCandidates.css';

const STATUS_OPTIONS = ['pending', 'promoted', 'rejected', 'superseded'];

function formatDate(value: string | null | undefined): string {
  if (!value) {
    return '-';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) {
    return '-';
  }
  if (typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : value.toFixed(4);
  }
  if (typeof value === 'string' || typeof value === 'boolean') {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function metricValue(metrics: Record<string, unknown>, keys: string[]): string {
  const match = keys.find((key) => metrics[key] !== undefined && metrics[key] !== null);
  return match ? formatValue(metrics[match]) : '-';
}

function confirmReview(
  action: 'approve' | 'reject',
  candidate: StrategyCandidate,
): boolean {
  const diffKeys = Object.keys(candidate.param_diff || {});
  const suffix = diffKeys.length ? ` Changed: ${diffKeys.join(', ')}` : '';
  return window.confirm(`${action === 'approve' ? 'Approve' : 'Reject'} ${candidate.candidate_id}?${suffix}`);
}

const StrategyCandidates: React.FC = () => {
  const [statusFilter, setStatusFilter] = useState('pending');
  const [candidates, setCandidates] = useState<StrategyCandidate[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selected, setSelected] = useState<StrategyCandidate | null>(null);
  const [reviewReason, setReviewReason] = useState('');
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [actionPending, setActionPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const loadCandidates = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await StrategyCandidateService.list({
        status: statusFilter,
        limit: 100,
      });
      setCandidates(response.candidates || []);
      setSelectedId((current) => {
        if (current && response.candidates.some((candidate) => candidate.candidate_id === current)) {
          return current;
        }
        return response.candidates[0]?.candidate_id || null;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load candidates');
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    loadCandidates();
  }, [loadCandidates]);

  useEffect(() => {
    let active = true;
    if (!selectedId) {
      setSelected(null);
      return undefined;
    }
    setDetailLoading(true);
    setError(null);
    StrategyCandidateService.get(selectedId)
      .then((candidate) => {
        if (active) {
          setSelected(candidate);
          setReviewReason('');
        }
      })
      .catch((err) => {
        if (active) {
          setError(err instanceof Error ? err.message : 'Failed to load candidate');
        }
      })
      .finally(() => {
        if (active) {
          setDetailLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [selectedId]);

  const diffRows = useMemo(
    () => Object.entries(selected?.param_diff || {}) as Array<[string, StrategyCandidateDiff]>,
    [selected],
  );

  const metricRows = useMemo(
    () => Object.entries(selected?.metrics || {}),
    [selected],
  );

  const refreshAfterReview = async (candidateId: string) => {
    await loadCandidates();
    setSelectedId(candidateId);
    const refreshed = await StrategyCandidateService.get(candidateId);
    setSelected(refreshed);
  };

  const handleReview = async (action: 'approve' | 'reject') => {
    if (!selected || actionPending || selected.status !== 'pending') {
      return;
    }
    if (!confirmReview(action, selected)) {
      return;
    }
    setActionPending(true);
    setError(null);
    setNotice(null);
    try {
      if (action === 'approve') {
        await StrategyCandidateService.approve(selected.candidate_id, reviewReason.trim());
      } else {
        await StrategyCandidateService.reject(selected.candidate_id, reviewReason.trim());
      }
      setNotice(`${selected.candidate_id} ${action === 'approve' ? 'promoted' : 'rejected'}`);
      await refreshAfterReview(selected.candidate_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${action} candidate`);
    } finally {
      setActionPending(false);
    }
  };

  return (
    <Gate requiredRoles={[Role.ADMIN]}>
      <div className="strategy-candidates-page">
        <div className="strategy-candidates-header">
          <div>
            <h1>Strategy Candidates</h1>
            <div className="strategy-candidates-count">{candidates.length} shown</div>
          </div>
          <div className="strategy-candidates-actions">
            <select
              aria-label="Candidate status"
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value)}
            >
              {STATUS_OPTIONS.map((status) => (
                <option key={status} value={status}>
                  {status}
                </option>
              ))}
            </select>
            <button type="button" className="icon-button" onClick={loadCandidates} disabled={loading}>
              <RefreshCw size={16} />
              Refresh
            </button>
          </div>
        </div>

        {error && <div className="strategy-candidates-error">{error}</div>}
        {notice && <div className="strategy-candidates-notice">{notice}</div>}

        <div className="strategy-candidates-grid">
          <section className="strategy-candidates-list" aria-label="Candidates">
            {loading ? (
              <div className="strategy-candidates-empty">Loading</div>
            ) : candidates.length === 0 ? (
              <div className="strategy-candidates-empty">No candidates</div>
            ) : (
              <table className="strategy-candidates-table">
                <thead>
                  <tr>
                    <th>Candidate</th>
                    <th>Strategy</th>
                    <th>Score</th>
                    <th>PnL</th>
                    <th>Win</th>
                    <th>Drawdown</th>
                    <th>Created</th>
                  </tr>
                </thead>
                <tbody>
                  {candidates.map((candidate) => (
                    <tr
                      key={candidate.candidate_id}
                      className={candidate.candidate_id === selectedId ? 'selected' : ''}
                      onClick={() => setSelectedId(candidate.candidate_id)}
                    >
                      <td>
                        <button type="button" className="row-select">
                          {candidate.candidate_id}
                        </button>
                      </td>
                      <td>
                        <span className="strategy-label">{candidate.strategy_id}</span>
                        <span className="account-label">{candidate.tenant_id} / {candidate.broker_account_id}</span>
                      </td>
                      <td>{metricValue(candidate.metrics, ['score', 'composite_score'])}</td>
                      <td>{metricValue(candidate.metrics, ['total_pnl', 'pnl', 'net_pnl'])}</td>
                      <td>{metricValue(candidate.metrics, ['win_rate', 'win_pct'])}</td>
                      <td>{metricValue(candidate.metrics, ['max_drawdown', 'drawdown'])}</td>
                      <td>{formatDate(candidate.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          <aside className="strategy-candidates-detail" aria-label="Candidate detail">
            {detailLoading ? (
              <div className="strategy-candidates-empty">Loading</div>
            ) : selected ? (
              <>
                <div className="candidate-detail-heading">
                  <div>
                    <h2>{selected.candidate_id}</h2>
                    <div className={`status-pill status-${selected.status}`}>
                      {selected.status}
                    </div>
                  </div>
                  <div className="review-buttons">
                    <button
                      type="button"
                      className="approve-button"
                      disabled={actionPending || selected.status !== 'pending'}
                      onClick={() => handleReview('approve')}
                    >
                      <CheckCircle size={16} />
                      Approve
                    </button>
                    <button
                      type="button"
                      className="reject-button"
                      disabled={actionPending || selected.status !== 'pending'}
                      onClick={() => handleReview('reject')}
                    >
                      <XCircle size={16} />
                      Reject
                    </button>
                  </div>
                </div>

                <label className="review-reason">
                  Reason
                  <input
                    value={reviewReason}
                    onChange={(event) => setReviewReason(event.target.value)}
                    placeholder="dashboard review"
                    disabled={actionPending || selected.status !== 'pending'}
                  />
                </label>

                <dl className="candidate-meta">
                  <div>
                    <dt>Config</dt>
                    <dd>{selected.strategy_config_id}</dd>
                  </div>
                  <div>
                    <dt>Optimizer</dt>
                    <dd>{selected.optimizer_version}</dd>
                  </div>
                  <div>
                    <dt>Window</dt>
                    <dd>{formatValue(selected.backtest_window)}</dd>
                  </div>
                  <div>
                    <dt>Reviewed</dt>
                    <dd>{selected.reviewed_by || '-'} {selected.reviewed_at ? formatDate(selected.reviewed_at) : ''}</dd>
                  </div>
                </dl>

                <h3>Parameter Diff</h3>
                {diffRows.length === 0 ? (
                  <div className="strategy-candidates-empty compact">No changed parameters</div>
                ) : (
                  <table className="diff-table">
                    <thead>
                      <tr>
                        <th>Key</th>
                        <th>Current</th>
                        <th>Candidate</th>
                      </tr>
                    </thead>
                    <tbody>
                      {diffRows.map(([key, diff]) => (
                        <tr key={key}>
                          <td>{key}</td>
                          <td>{formatValue(diff.current)}</td>
                          <td>{formatValue(diff.candidate)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}

                <h3>Metrics</h3>
                <div className="metrics-grid">
                  {metricRows.map(([key, value]) => (
                    <div className="metric-cell" key={key}>
                      <span>{key}</span>
                      <strong>{formatValue(value)}</strong>
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <div className="strategy-candidates-empty">Select a candidate</div>
            )}
          </aside>
        </div>
      </div>
    </Gate>
  );
};

export default StrategyCandidates;
