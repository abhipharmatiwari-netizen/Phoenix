import React, { FormEvent, useCallback, useEffect, useMemo, useState } from 'react';
import Gate from '../auth/Gate';
import { AdminService, DefaultService } from '../client';
import LoadingSpinner from '../components/shared/LoadingSpinner';
import StatusBadge from '../components/shared/StatusBadge';
import { coerceRecord, compactJson, displayValue, formatDateTime } from '../lib/consoleUtils';
import { Role } from '../lib/rbac';

interface StrategyRow extends Record<string, unknown> {
  id: string;
  enabled: boolean;
  mode: string;
  instruments: string;
  trading_windows: string;
  lot_sizing: string;
  risk_params: string;
  last_signal: string;
  raw: unknown;
}

interface ToggleDialog {
  strategyId: string;
  enabled: boolean;
}

function entriesFromSnapshot(snapshot: unknown): Array<[string, unknown]> {
  if (Array.isArray(snapshot)) {
    return snapshot.map((item, index) => {
      const record = coerceRecord(item);
      return [String(record.strategy_id || record.name || `strategy_${index + 1}`), item];
    });
  }
  const record = coerceRecord(snapshot);
  return Object.entries(record);
}

function strategyEnabled(value: unknown): boolean {
  if (typeof value === 'boolean') {
    return value;
  }
  const record = coerceRecord(value);
  return Boolean(record.enabled ?? record.is_enabled ?? record.active);
}

function instrumentListFor(strategyId: string, instruments: unknown): string {
  const rows = entriesFromSnapshot(instruments);
  const matched = rows
    .filter(([, value]) => {
      const record = coerceRecord(value);
      const allowed = record.allowed_strategies;
      return Array.isArray(allowed) && allowed.map(String).includes(strategyId);
    })
    .map(([name]) => name);
  return matched.length ? matched.join(', ') : 'Unknown';
}

const Strategies: React.FC = () => {
  const [strategies, setStrategies] = useState<unknown>(null);
  const [instruments, setInstruments] = useState<unknown>(null);
  const [selection, setSelection] = useState<unknown[]>([]);
  const [tradeMode, setTradeMode] = useState('UNKNOWN');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [dialog, setDialog] = useState<ToggleDialog | null>(null);
  const [reason, setReason] = useState('');
  const [stepUpToken, setStepUpToken] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [strategyResp, instrumentResp, selectionResp, healthResp] = await Promise.all([
        AdminService.getStrategies(),
        AdminService.getInstruments(),
        AdminService.getStrategySelection().catch(() => ({ strategy_selection: [] })),
        DefaultService.getOperatorHealthSummary().catch(() => null),
      ]);
      setStrategies(strategyResp.strategies);
      setInstruments(instrumentResp.instruments);
      setSelection(Array.isArray(selectionResp.strategy_selection) ? selectionResp.strategy_selection : []);
      setTradeMode(String(healthResp?.summary.trade_mode || healthResp?.summary.operating_mode || 'UNKNOWN').toUpperCase());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load strategy state');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const rows = useMemo<StrategyRow[]>(() => {
    const lastSignalByStrategy = new Map<string, string>();
    selection.forEach((item) => {
      const record = coerceRecord(item);
      const selected = Array.isArray(record.selected_strategies)
        ? record.selected_strategies.map(String)
        : [];
      selected.forEach((strategyId) => {
        lastSignalByStrategy.set(
          strategyId,
          `${displayValue(record.regime, 'Unknown')} at ${formatDateTime(record.updated_at)}`,
        );
      });
    });
    return entriesFromSnapshot(strategies).map(([id, value]) => {
      const record = coerceRecord(value);
      return {
        id,
        enabled: strategyEnabled(value),
        mode: tradeMode,
        instruments: instrumentListFor(id, instruments),
        trading_windows: displayValue(record.trading_windows || record.windows, 'Unknown'),
        lot_sizing: displayValue(record.lot_size || record.lot_sizing || record.quantity, 'Unknown'),
        risk_params: displayValue(record.risk_params || record.risk || record.params, 'Unknown'),
        last_signal: lastSignalByStrategy.get(id) || 'Unknown',
        raw: value,
      };
    });
  }, [instruments, selection, strategies, tradeMode]);

  const submitToggle = async (event: FormEvent) => {
    event.preventDefault();
    if (!dialog || !reason.trim()) {
      return;
    }
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      await AdminService.toggleStrategy({
        name: dialog.strategyId,
        enabled: dialog.enabled,
        reason: reason.trim(),
        step_up_token: stepUpToken.trim() || null,
      });
      setSuccess(`${dialog.strategyId} ${dialog.enabled ? 'enabled' : 'disabled'}`);
      setDialog(null);
      setReason('');
      setStepUpToken('');
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Strategy mutation failed');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Gate requiredRoles={[Role.READONLY]}>
      <div className="console-page">
        <div className="page-header">
          <div>
            <h1>Strategies</h1>
            <p>Enabled configs, instruments, operating mode, risk fields, and last selection evidence.</p>
          </div>
          <button className="secondary-button" type="button" onClick={load} disabled={loading || saving}>
            Refresh
          </button>
        </div>

        {error && <div className="notice notice--blocked">{error}</div>}
        {success && <div className="notice notice--healthy">{success}</div>}

        {loading ? (
          <LoadingSpinner />
        ) : (
          <div className="responsive-table">
            <table>
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th>Status</th>
                  <th>Mode</th>
                  <th>Instruments</th>
                  <th>Trading Windows</th>
                  <th>Lot Sizing</th>
                  <th>Risk Params</th>
                  <th>Last Signal</th>
                  <th>Mutation</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td data-label="Strategy">
                      <strong>{row.id}</strong>
                      <details>
                        <summary>Evidence</summary>
                        <pre className="evidence-block">{compactJson(row.raw)}</pre>
                      </details>
                    </td>
                    <td data-label="Status">
                      <StatusBadge status={row.enabled ? 'ok' : 'unknown'} label={row.enabled ? 'Enabled' : 'Disabled'} />
                    </td>
                    <td data-label="Mode">
                      <span className={`env-badge env-badge--${row.mode.toLowerCase()}`}>{row.mode}</span>
                    </td>
                    <td data-label="Instruments">{row.instruments}</td>
                    <td data-label="Trading Windows">{row.trading_windows}</td>
                    <td data-label="Lot Sizing">{row.lot_sizing}</td>
                    <td data-label="Risk Params">{row.risk_params}</td>
                    <td data-label="Last Signal">{row.last_signal}</td>
                    <td data-label="Mutation">
                      <Gate requiredRoles={[Role.OPERATOR]}>
                        <button
                          className={row.enabled ? 'danger-button' : 'secondary-button'}
                          type="button"
                          onClick={() => setDialog({ strategyId: row.id, enabled: !row.enabled })}
                          disabled={saving}
                        >
                          {row.enabled ? 'Disable' : 'Enable'}
                        </button>
                      </Gate>
                    </td>
                  </tr>
                ))}
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={9}>No strategy configs returned by the authenticated admin API.</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}

        {dialog && (
          <div className="modal-backdrop" role="presentation">
            <form className="console-modal" onSubmit={submitToggle}>
              <h2>{dialog.enabled ? 'Enable' : 'Disable'} {dialog.strategyId}</h2>
              <p>This audited mutation changes whether Phoenix can use this strategy. Unknown backend state must be investigated before proceeding.</p>
              <label>
                Reason
                <textarea value={reason} onChange={(event) => setReason(event.target.value)} rows={3} required />
              </label>
              {tradeMode === 'LIVE' && (
                <label>
                  Step-up token
                  <input value={stepUpToken} onChange={(event) => setStepUpToken(event.target.value)} />
                </label>
              )}
              <div className="modal-actions">
                <button className="secondary-button" type="button" onClick={() => setDialog(null)} disabled={saving}>
                  Cancel
                </button>
                <button className="danger-button" type="submit" disabled={saving || !reason.trim() || (tradeMode === 'LIVE' && !stepUpToken.trim())}>
                  {saving ? 'Applying...' : 'Confirm'}
                </button>
              </div>
            </form>
          </div>
        )}
      </div>
    </Gate>
  );
};

export default Strategies;
