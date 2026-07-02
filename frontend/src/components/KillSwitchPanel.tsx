import React, { FormEvent, useCallback, useEffect, useMemo, useState } from 'react';
import {
  AdminService,
  BreakGlassFlattenPayload,
  KillSwitchCancelAllResponse,
  KillSwitchRecord,
  KillSwitchService,
  KillSwitchStateResponse,
} from '../client';
import StatusBadge from './shared/StatusBadge';

const POLL_INTERVAL_MS = 10_000;

type ActionKind =
  | 'trip'
  | 'upgrade_hard'
  | 'repair'
  | 'request_clear'
  | 'confirm_clear'
  | 'rearm'
  | 'password_clear'
  | 'cancel_all'
  | 'break_glass';

interface PendingAction {
  kind: ActionKind;
  title: string;
  body: string;
  requiresStepUp?: boolean;
  requiresPassword?: boolean;
  hardTrip?: boolean;
}

const EMPTY_BREAK_GLASS: BreakGlassFlattenPayload = {
  tenant_id: '',
  broker_account_id: '',
  underlying: '',
  expiry: '',
  strike: '',
  option_right: 'CE',
  product_type: 'INTRADAY',
  reason: '',
  step_up_token: null,
};

const KillSwitchPanel: React.FC = () => {
  const [stateResp, setStateResp] = useState<KillSwitchStateResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [cancelResult, setCancelResult] = useState<KillSwitchCancelAllResponse | null>(null);
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [reason, setReason] = useState('');
  const [stepUpToken, setStepUpToken] = useState('');
  const [overridePassword, setOverridePassword] = useState('');
  const [breakGlass, setBreakGlass] = useState<BreakGlassFlattenPayload>(EMPTY_BREAK_GLASS);

  const fetchState = useCallback(async () => {
    try {
      const resp = await KillSwitchService.getState();
      setStateResp(resp);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch kill-switch state');
      setStateResp(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchState();
    const timer = window.setInterval(fetchState, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [fetchState]);

  const globalRecord = useMemo<KillSwitchRecord | null>(() => {
    const records = stateResp?.records || [];
    return records.find((record) => record.scope === 'GLOBAL' && record.scope_id === 'GLOBAL')
      || records.find((record) => record.scope === 'GLOBAL')
      || null;
  }, [stateResp]);

  const legacyActive = Boolean(stateResp?.legacy_kill_switch?.active);
  const legacyFallbackActive = stateResp?.source === 'risk_manager' && Boolean(stateResp?.kill_switch_activated);
  const durableBlocking = Boolean(globalRecord && ['TRIPPED', 'CLEAR_PENDING'].includes(globalRecord.state));
  const divergent = Boolean(stateResp?.divergence?.divergent) || (!durableBlocking && (legacyActive || legacyFallbackActive));
  const globalState = stateResp == null
    ? 'UNKNOWN'
    : divergent
      ? 'DIVERGENT'
      : globalRecord?.state || 'INACTIVE';
  const tradeMode = String(stateResp?.trade_mode || 'UNKNOWN').toUpperCase();
  const liveMode = tradeMode === 'LIVE';
  const activeCount = Number(stateResp?.active_count ?? 0);

  const resetDialog = () => {
    setPending(null);
    setReason('');
    setStepUpToken('');
    setOverridePassword('');
    setBreakGlass(EMPTY_BREAK_GLASS);
  };

  const openAction = (action: PendingAction) => {
    setPending(action);
    setError(null);
    setFeedback(null);
    if (action.kind === 'break_glass') {
      setBreakGlass({ ...EMPTY_BREAK_GLASS });
    }
  };

  const runAction = async (event: FormEvent) => {
    event.preventDefault();
    if (!pending) return;
    const trimmedReason = (pending.kind === 'break_glass' ? breakGlass.reason : reason).trim();
    if (!trimmedReason) {
      setError('Reason is required');
      return;
    }
    if (pending.requiresStepUp && !stepUpToken.trim()) {
      setError('Step-up token is required for this LIVE action');
      return;
    }
    if (pending.requiresPassword && !overridePassword) {
      setError('Override password is required');
      return;
    }

    setBusy(true);
    setError(null);
    setFeedback(null);
    try {
      switch (pending.kind) {
        case 'repair':
          await KillSwitchService.repairDurableFromLegacy({ reason: trimmedReason, block_exits: false });
          setFeedback('Durable GLOBAL kill switch repaired from legacy state.');
          break;
        case 'trip':
        case 'upgrade_hard':
          await KillSwitchService.trip({
            scope: 'GLOBAL',
            scope_id: 'GLOBAL',
            reason: trimmedReason,
            block_exits: pending.kind === 'upgrade_hard' || Boolean(pending.hardTrip),
          });
          setFeedback(pending.kind === 'upgrade_hard' ? 'Kill switch upgraded to HARD.' : 'Kill switch tripped.');
          break;
        case 'request_clear':
          await KillSwitchService.requestClear({
            scope: 'GLOBAL',
            scope_id: 'GLOBAL',
            reason_code: trimmedReason,
            break_glass: false,
          });
          setFeedback('Clear requested. Confirm clear after independent evidence review.');
          break;
        case 'confirm_clear':
          await KillSwitchService.confirmClear({
            scope: 'GLOBAL',
            scope_id: 'GLOBAL',
            reason: trimmedReason,
            step_up_token: stepUpToken.trim() || null,
          });
          setFeedback('Kill switch clear confirmed.');
          break;
        case 'rearm':
          await KillSwitchService.rearm({
            scope: 'GLOBAL',
            scope_id: 'GLOBAL',
            reason: trimmedReason,
            step_up_token: stepUpToken.trim() || null,
          });
          setFeedback('Kill switch rearmed.');
          break;
        case 'password_clear':
          await KillSwitchService.clearWithPassword({
            scope: 'GLOBAL',
            scope_id: 'GLOBAL',
            password: overridePassword,
            reason: trimmedReason,
          });
          setFeedback('Override clear submitted and backend state refreshed.');
          break;
        case 'cancel_all': {
          const result = await KillSwitchService.cancelAll({ reason: trimmedReason });
          setCancelResult(result);
          setFeedback(`Cancel-all ${result.status}: attempted=${result.attempted}, cancelled=${result.cancelled}, failed=${result.failed}.`);
          break;
        }
        case 'break_glass':
          await AdminService.breakGlassFlatten({
            ...breakGlass,
            reason: trimmedReason,
            step_up_token: stepUpToken.trim() || null,
          });
          setFeedback('Break-glass flatten submitted.');
          break;
      }
      resetDialog();
      await fetchState();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action failed');
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return <div className="evidence-panel">Loading kill-switch state...</div>;
  }

  const stateStatus = globalState === 'INACTIVE' ? 'ok' : globalState === 'UNKNOWN' ? 'unknown' : 'error';
  const stateDependentDisabled = busy || globalState === 'UNKNOWN';
  const cancelEnabled = !stateDependentDisabled && ['TRIPPED', 'CLEAR_PENDING'].includes(globalState);

  return (
    <section className={`safety-panel safety-panel--${globalState.toLowerCase()}`}>
      <div className="safety-panel__header">
        <div>
          <h2>Global Kill Switch</h2>
          <p>Durable Postgres-backed state is authoritative. Unknown and divergent states are blocked.</p>
        </div>
        <div className="safety-panel__badges">
          <StatusBadge status={stateStatus} label={globalState} />
          <span className={`env-badge env-badge--${tradeMode.toLowerCase()}`}>{tradeMode}</span>
        </div>
      </div>

      {error && <div className="notice notice--blocked">{error}</div>}
      {feedback && <div className="notice notice--healthy">{feedback}</div>}
      {globalState === 'UNKNOWN' && (
        <div className="notice notice--blocked">
          Durable kill-switch state is unavailable. State-dependent actions are disabled until authoritative state returns.
        </div>
      )}
      {globalState === 'DIVERGENT' && (
        <div className="notice notice--blocked">
          Legacy and durable kill-switch state diverge. Repair or trip durable state before attempting clear/rearm.
        </div>
      )}

      <div className="safety-state-grid">
        <div><span>Source</span><strong>{stateResp?.source || 'unavailable'}</strong></div>
        <div><span>Active Records</span><strong>{activeCount}</strong></div>
        <div><span>Block Exits</span><strong>{globalRecord?.block_exits ? 'Yes' : 'No / Unknown'}</strong></div>
        <div><span>Last Actor</span><strong>{globalRecord?.tripped_by || globalRecord?.cleared_by || 'Unknown'}</strong></div>
        <div><span>Last Change</span><strong>{globalRecord?.updated_at ? new Date(globalRecord.updated_at).toLocaleString() : 'Unknown'}</strong></div>
        <div><span>Reason</span><strong>{globalRecord?.trip_reason || globalRecord?.clear_reason || stateResp?.legacy_kill_switch?.reason || 'Unknown'}</strong></div>
      </div>

      <div className="danger-zone">
        <button
          className="danger-button"
          type="button"
          disabled={stateDependentDisabled || (globalState !== 'INACTIVE' && globalState !== 'DIVERGENT')}
          onClick={() => openAction({
            kind: globalState === 'DIVERGENT' ? 'repair' : 'trip',
            title: globalState === 'DIVERGENT' ? 'Durable repair' : 'Trip kill switch',
            body: globalState === 'DIVERGENT'
              ? 'Create a durable GLOBAL trip from active legacy kill-switch state.'
              : 'Block new entry orders globally. Use HARD only when exits must also be blocked.',
          })}
        >
          Trip Kill Switch
        </button>
        <button
          className="danger-button"
          type="button"
          disabled={stateDependentDisabled || globalState !== 'TRIPPED' || Boolean(globalRecord?.block_exits)}
          onClick={() => openAction({ kind: 'upgrade_hard', title: 'Upgrade to HARD', body: 'Block all orders including exits. Manual flatten may be required.' })}
        >
          Upgrade HARD
        </button>
        <button
          className="secondary-button"
          type="button"
          disabled={stateDependentDisabled || globalState !== 'TRIPPED'}
          onClick={() => openAction({ kind: 'request_clear', title: 'Request clear', body: 'Move TRIPPED to CLEAR_PENDING after evidence review.' })}
        >
          Request Clear
        </button>
        <button
          className="danger-button"
          type="button"
          disabled={stateDependentDisabled || globalState !== 'CLEAR_PENDING'}
          onClick={() => openAction({ kind: 'confirm_clear', title: 'Confirm clear', body: 'Confirm clear after independent evidence review.', requiresStepUp: liveMode })}
        >
          Confirm Clear
        </button>
        <button
          className="danger-button"
          type="button"
          disabled={stateDependentDisabled || globalState !== 'CLEARED'}
          onClick={() => openAction({ kind: 'rearm', title: 'Rearm', body: 'Return CLEARED to INACTIVE.', requiresStepUp: liveMode })}
        >
          Rearm
        </button>
        <button
          className="danger-button"
          type="button"
          disabled={stateDependentDisabled || globalState !== 'TRIPPED'}
          onClick={() => openAction({ kind: 'password_clear', title: 'Override clear', body: 'Use the vault-backed override password. Backend validation still fails closed.', requiresPassword: true })}
        >
          Override Clear
        </button>
        <button
          className="danger-button"
          type="button"
          disabled={!cancelEnabled}
          onClick={() => openAction({ kind: 'cancel_all', title: 'Cancel all open broker orders', body: 'Cancel every non-terminal broker order. This does not flatten filled exposure.' })}
        >
          Cancel All Orders
        </button>
        <button
          className="danger-button"
          type="button"
          disabled={busy}
          onClick={() => openAction({ kind: 'break_glass', title: 'Emergency square-off / break-glass exit', body: 'Submit a real routed exit for one authoritative contract.', requiresStepUp: liveMode })}
        >
          Break-Glass Exit
        </button>
      </div>

      {cancelResult && (
        <div className="notice notice--warning">
          Cancel evidence: attempted={cancelResult.attempted}, cancelled={cancelResult.cancelled}, failed={cancelResult.failed}, skipped={cancelResult.skipped}, raced_filled={cancelResult.raced_filled ?? 0}, refresh_failures={cancelResult.refresh_failures ?? 0}.
        </div>
      )}

      {pending && (
        <div className="modal-backdrop" role="presentation">
          <form className="console-modal" onSubmit={runAction}>
            <h2>{pending.title}</h2>
            <p>{pending.body}</p>
            {pending.kind === 'break_glass' ? (
              <div className="form-grid">
                {(['tenant_id', 'broker_account_id', 'underlying', 'expiry', 'strike', 'option_right', 'product_type'] as const).map((key) => (
                  <label key={key}>
                    {key.replace(/_/g, ' ')}
                    <input
                      value={String(breakGlass[key] || '')}
                      onChange={(event) => setBreakGlass({ ...breakGlass, [key]: event.target.value })}
                      required
                    />
                  </label>
                ))}
                <label className="form-grid__wide">
                  Reason
                  <textarea value={breakGlass.reason} onChange={(event) => setBreakGlass({ ...breakGlass, reason: event.target.value })} required rows={3} />
                </label>
              </div>
            ) : (
              <label>
                Reason
                <textarea value={reason} onChange={(event) => setReason(event.target.value)} required rows={3} />
              </label>
            )}
            {pending.requiresStepUp && (
              <label>
                Step-up token
                <input value={stepUpToken} onChange={(event) => setStepUpToken(event.target.value)} required />
              </label>
            )}
            {pending.requiresPassword && (
              <label>
                Override password
                <input type="password" value={overridePassword} onChange={(event) => setOverridePassword(event.target.value)} required autoComplete="off" />
              </label>
            )}
            <div className="modal-actions">
              <button className="secondary-button" type="button" onClick={resetDialog} disabled={busy}>Cancel</button>
              <button className="danger-button" type="submit" disabled={busy}>
                {busy ? 'Working...' : 'Confirm'}
              </button>
            </div>
          </form>
        </div>
      )}
    </section>
  );
};

export default KillSwitchPanel;
