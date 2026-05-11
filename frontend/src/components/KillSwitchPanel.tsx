import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  KillSwitchService,
  KillSwitchRecord,
  KillSwitchStateResponse,
  KillSwitchCancelAllResponse,
} from '../client';

// Issue #238: dashboard kill-switch toggle on the Safety page.
//
// Operators trip / clear / rearm / cancel-all from this panel. Backend
// state lives in the durable Postgres-backed KillSwitchManager and is
// re-fetched after every toggle so the UI cannot show "tripped" while
// the backend disagrees. All destructive controls require an explicit
// non-empty operator-entered reason and a confirmation step.

const POLL_INTERVAL_MS = 10_000;

const STATE_COLOURS: Record<KillSwitchRecord['state'], { bg: string; fg: string; border: string }> = {
  INACTIVE: { bg: '#f0fdf4', fg: '#166534', border: '#16a34a' },
  TRIPPED: { bg: '#fef2f2', fg: '#991b1b', border: '#dc2626' },
  CLEAR_PENDING: { bg: '#fef3c7', fg: '#92400e', border: '#f59e0b' },
  CLEARED: { bg: '#eff6ff', fg: '#1e3a8a', border: '#2563eb' },
};

interface ConfirmDialog {
  title: string;
  prompt: string;
  reasonLabel: string;
  hardOption?: boolean;
  // PR #240 round-2 review P1: the dashboard must NOT auto-mint a
  // step-up token from the already-authenticated admin session — that
  // defeats the LIVE-mode protection that step-up is meant to add.
  // Operators must supply an independently-obtained step-up token
  // (e.g. via a separate CLI ceremony or future MFA channel). When
  // ``requireStepUpToken`` is true the dialog renders a token-input
  // field and refuses to confirm until it is filled.
  requireStepUpToken?: boolean;
  stepUpInstructions?: string;
  onConfirm: (reason: string, hard: boolean, stepUpToken: string) => Promise<void>;
}

const KillSwitchPanel: React.FC = () => {
  const [stateResp, setStateResp] = useState<KillSwitchStateResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionFeedback, setActionFeedback] = useState<string | null>(null);
  const [cancelResult, setCancelResult] = useState<KillSwitchCancelAllResponse | null>(null);
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialog | null>(null);
  const [reasonInput, setReasonInput] = useState('');
  const [hardInput, setHardInput] = useState(false);
  const [stepUpTokenInput, setStepUpTokenInput] = useState('');

  const fetchState = useCallback(async () => {
    try {
      const resp = await KillSwitchService.getState();
      setStateResp(resp);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch kill-switch state');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    const tick = async () => {
      await fetchState();
      if (!active) return;
    };
    tick();
    const timer = window.setInterval(() => {
      void fetchState();
    }, POLL_INTERVAL_MS);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [fetchState]);

  const globalRecord = useMemo<KillSwitchRecord | null>(() => {
    if (!stateResp?.records) return null;
    const records = stateResp.records;
    const exact = records.find((r) => r.scope === 'GLOBAL' && r.scope_id === 'GLOBAL');
    if (exact) return exact;
    const anyGlobal = records.find((r) => r.scope === 'GLOBAL');
    return anyGlobal || null;
  }, [stateResp]);

  // PR #240 round-3 review P2: when the durable manager has no GLOBAL
  // record but the legacy stream-path kill switch is active (issue
  // #222 divergence), surface "TRIPPED" so the panel doesn't mislead
  // the operator into thinking no protection is in place. The
  // backend ``state`` response already includes ``legacy_kill_switch``
  // and ``divergence`` for exactly this case.
  const legacyActive = Boolean(stateResp?.legacy_kill_switch?.legacy_active);
  const divergent = Boolean(stateResp?.divergence?.divergent);
  const globalState: KillSwitchRecord['state'] = globalRecord?.state
    || (legacyActive || divergent ? 'TRIPPED' : 'INACTIVE');
  const colours = STATE_COLOURS[globalState];

  // PR #240 round-3 review P2: only require a step-up token when the
  // backend will actually enforce it (LIVE mode). In PAPER/SHADOW the
  // backend explicitly accepts an empty token, so the UI must not
  // block the operator's recovery flow.
  const isLive = String(stateResp?.trade_mode || '').toUpperCase() === 'LIVE';

  const closeDialog = () => {
    setConfirmDialog(null);
    setReasonInput('');
    setHardInput(false);
    setStepUpTokenInput('');
  };

  const runDialog = async () => {
    if (!confirmDialog) return;
    const trimmed = reasonInput.trim();
    if (!trimmed) {
      setError('Reason is required');
      return;
    }
    const trimmedToken = stepUpTokenInput.trim();
    if (confirmDialog.requireStepUpToken && !trimmedToken) {
      setError(
        'Step-up token is required for this action in LIVE mode. '
        + 'Obtain one via the operator runbook before retrying.',
      );
      return;
    }
    setBusy(true);
    setError(null);
    setActionFeedback(null);
    try {
      await confirmDialog.onConfirm(trimmed, hardInput, trimmedToken);
      await fetchState();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action failed');
    } finally {
      setBusy(false);
      closeDialog();
    }
  };

  // PR #240 round-2/round-3 review P1: instructions for obtaining a
  // step-up token via a separate ceremony. The token is ACTOR-BOUND
  // (consumed with ``actor=ctx.caller``), so the operator must issue
  // it with THEIR OWN admin bearer token — using a different
  // operator's token causes an actor-mismatch rejection. The curl
  // must also include ``Content-Type: application/json`` because the
  // ``-d`` flag defaults to form-encoded.
  const stepUpInstructions = (actionClass: string) =>
    `In LIVE mode this action requires a separately-issued step-up token.
Obtain one via the operator runbook — use YOUR OWN admin token
(tokens are actor-bound; a different operator's token will be rejected):
  curl -X POST $PHOENIX/admin/step-up/issue \\
       -H "Authorization: Bearer $YOUR_ADMIN_TOKEN" \\
       -H "Content-Type: application/json" \\
       -d '{"action_class":"${actionClass}","resource_id":"GLOBAL"}'
Paste the returned token_id below.`;

  // Trip — operator confirms and provides reason; SOFT vs HARD selectable.
  const onTrip = () => {
    setConfirmDialog({
      title: 'Trip GLOBAL kill switch',
      prompt: 'This blocks new entry orders across every account. HARD trip also blocks exits.',
      reasonLabel: 'Reason',
      hardOption: true,
      onConfirm: async (reason, hard) => {
        await KillSwitchService.trip({
          scope: 'GLOBAL',
          scope_id: 'GLOBAL',
          reason,
          block_exits: hard,
        });
        setActionFeedback(`Kill switch tripped (${hard ? 'HARD' : 'SOFT'}).`);
      },
    });
  };

  // PR #240 round-1 review P2: dedicated handler for the "Upgrade
  // SOFT → HARD" button. The generic trip dialog defaulted ``hard``
  // to false, which either 409s on an existing SOFT trip or
  // accidentally downgrades a HARD trip back to SOFT. This handler
  // forces ``block_exits=true`` and hides the SOFT/HARD checkbox so
  // confirming the dialog reliably upgrades to HARD.
  const onUpgradeToHard = () => {
    setConfirmDialog({
      title: 'Upgrade SOFT → HARD',
      prompt: 'Switches the existing TRIPPED kill switch from SOFT (entries-only) to HARD (blocks ALL orders including exits). Operator must manually flatten broker positions after this.',
      reasonLabel: 'Reason',
      hardOption: false,
      onConfirm: async (reason) => {
        await KillSwitchService.trip({
          scope: 'GLOBAL',
          scope_id: 'GLOBAL',
          reason,
          block_exits: true,
        });
        setActionFeedback('Kill switch upgraded to HARD. All orders now blocked.');
      },
    });
  };

  const onRequestClear = () => {
    setConfirmDialog({
      title: 'Request CLEAR for GLOBAL kill switch',
      prompt: 'Moves the kill switch from TRIPPED → CLEAR_PENDING. A separate confirmation step is required to fully clear.',
      reasonLabel: 'Reason code',
      onConfirm: async (reason) => {
        await KillSwitchService.requestClear({
          scope: 'GLOBAL',
          scope_id: 'GLOBAL',
          reason_code: reason,
        });
        setActionFeedback('Clear requested. State now CLEAR_PENDING.');
      },
    });
  };

  const onConfirmClear = () => {
    // PR #240 round-2/round-3 review P1+P2: confirm-clear restores
    // LIVE entry eligibility, so LIVE requires a separately-issued
    // step-up token. In PAPER/SHADOW the backend accepts an empty
    // token; gate the UI requirement on the actual ``trade_mode``
    // returned by the state endpoint.
    setConfirmDialog({
      title: 'Confirm CLEAR for GLOBAL kill switch',
      prompt: isLive
        ? 'Moves CLEAR_PENDING → CLEARED. This re-allows new entry orders in LIVE mode and requires a separately-issued step-up token.'
        : 'Moves CLEAR_PENDING → CLEARED. After this, rearm to return to INACTIVE.',
      reasonLabel: 'Reason / acknowledgement',
      requireStepUpToken: isLive,
      stepUpInstructions: isLive ? stepUpInstructions('kill_switch_clear') : undefined,
      onConfirm: async (reason, _hard, stepUpToken) => {
        // PR #240 round-3 review P2: send the operator-entered
        // reason so the audit event records the typed justification.
        await KillSwitchService.confirmClear({
          scope: 'GLOBAL',
          scope_id: 'GLOBAL',
          step_up_token: stepUpToken || null,
          reason,
        });
        setActionFeedback('Clear confirmed. State now CLEARED. Use Rearm to return to INACTIVE.');
      },
    });
  };

  const onRearm = () => {
    setConfirmDialog({
      title: 'Rearm GLOBAL kill switch',
      prompt: isLive
        ? 'Moves CLEARED → INACTIVE. In LIVE mode this requires a separately-issued step-up token.'
        : 'Moves CLEARED → INACTIVE.',
      reasonLabel: 'Reason / acknowledgement',
      requireStepUpToken: isLive,
      stepUpInstructions: isLive ? stepUpInstructions('kill_switch_rearm') : undefined,
      onConfirm: async (reason, _hard, stepUpToken) => {
        await KillSwitchService.rearm({
          scope: 'GLOBAL',
          scope_id: 'GLOBAL',
          step_up_token: stepUpToken || null,
          reason,
        });
        setActionFeedback('Rearmed. State now INACTIVE.');
      },
    });
  };

  const onCancelAll = () => {
    // PR #240 round-1 review P2: clear any stale prior cancel-all
    // result BEFORE opening the dialog so an operator doesn't see a
    // green summary next to a red error from this attempt.
    setCancelResult(null);
    setConfirmDialog({
      title: 'Cancel ALL open broker orders',
      prompt: 'Iterates every registered runner and cancels every non-terminal order via the broker adapter. Idempotent — already-cancelled orders are skipped, not failed. Broker-side FILL races are reported separately as raced_filled so the operator knows new exposure may need manual flattening.',
      reasonLabel: 'Reason',
      onConfirm: async (reason) => {
        const resp = await KillSwitchService.cancelAll({ reason });
        setCancelResult(resp);
        setActionFeedback(
          `Cancel-all ${resp.status}: attempted=${resp.attempted}, `
          + `cancelled=${resp.cancelled}, failed=${resp.failed}, `
          + `skipped=${resp.skipped}, raced_filled=${resp.raced_filled ?? 0}.`,
        );
      },
    });
  };

  if (loading) {
    return <div style={{ padding: '1rem', color: '#6b7280' }}>Loading kill-switch state…</div>;
  }

  return (
    <div
      style={{
        border: `2px solid ${colours.border}`,
        borderRadius: 12,
        padding: '1.25rem',
        marginBottom: '1.5rem',
        backgroundColor: colours.bg,
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: '0.75rem',
        }}
      >
        <h2 style={{ margin: 0, fontSize: '1.125rem', color: colours.fg }}>
          Global Kill Switch
        </h2>
        <span
          style={{
            padding: '0.25rem 0.75rem',
            borderRadius: 999,
            backgroundColor: colours.fg,
            color: '#fff',
            fontSize: '0.875rem',
            fontWeight: 600,
            letterSpacing: '0.025em',
          }}
        >
          {globalState}
          {globalRecord?.block_exits ? ' · HARD' : globalRecord ? ' · SOFT' : ''}
        </span>
      </div>

      {error && (
        <div
          style={{
            color: '#991b1b',
            backgroundColor: '#fef2f2',
            padding: '0.5rem 0.75rem',
            borderRadius: 6,
            marginBottom: '0.75rem',
            fontSize: '0.875rem',
          }}
        >
          {error}
        </div>
      )}

      {actionFeedback && (
        <div
          style={{
            color: '#1e3a8a',
            backgroundColor: '#eff6ff',
            padding: '0.5rem 0.75rem',
            borderRadius: 6,
            marginBottom: '0.75rem',
            fontSize: '0.875rem',
          }}
        >
          {actionFeedback}
        </div>
      )}

      <div style={{ fontSize: '0.875rem', color: '#374151', marginBottom: '0.75rem' }}>
        {globalRecord ? (
          <>
            <div>
              <strong>Last actor:</strong>{' '}
              {globalRecord.tripped_by || globalRecord.cleared_by || '—'}
            </div>
            <div>
              <strong>Last change:</strong>{' '}
              {globalRecord.updated_at
                ? new Date(globalRecord.updated_at).toLocaleString()
                : '—'}
            </div>
            {globalRecord.trip_reason && (
              <div>
                <strong>Trip reason:</strong> {globalRecord.trip_reason}
              </div>
            )}
            {globalRecord.clear_reason && (
              <div>
                <strong>Clear reason:</strong> {globalRecord.clear_reason}
              </div>
            )}
          </>
        ) : (
          <em>No active kill-switch record. Switch is INACTIVE.</em>
        )}
      </div>

      <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
        {globalState === 'INACTIVE' && (
          <button
            type="button"
            onClick={onTrip}
            disabled={busy}
            style={btnStyle('#dc2626')}
          >
            Trip Kill Switch
          </button>
        )}
        {globalState === 'TRIPPED' && (
          <>
            {/* PR #240 round-1 review P2: only show the upgrade
                button when the current trip is SOFT — once HARD,
                upgrading again is a no-op / risk of downgrade. */}
            {!globalRecord?.block_exits && (
              <button
                type="button"
                onClick={onUpgradeToHard}
                disabled={busy}
                style={btnStyle('#dc2626')}
              >
                Upgrade SOFT → HARD
              </button>
            )}
            <button
              type="button"
              onClick={onRequestClear}
              disabled={busy}
              style={btnStyle('#f59e0b')}
            >
              Request Clear
            </button>
          </>
        )}
        {globalState === 'CLEAR_PENDING' && (
          <button
            type="button"
            onClick={onConfirmClear}
            disabled={busy}
            style={btnStyle('#2563eb')}
          >
            Confirm Clear
          </button>
        )}
        {globalState === 'CLEARED' && (
          <button
            type="button"
            onClick={onRearm}
            disabled={busy}
            style={btnStyle('#16a34a')}
          >
            Rearm (→ INACTIVE)
          </button>
        )}
        {/* PR #240 round-2 review P2: only enable cancel-all when the
            kill switch is actively blocking new placements. Cancelling
            open orders while INACTIVE/CLEARED leaves Phoenix free to
            place fresh orders during the bulk-cancel window. The
            operator must Trip first to block placements. */}
        <button
          type="button"
          onClick={onCancelAll}
          disabled={
            busy
            || (globalState !== 'TRIPPED' && globalState !== 'CLEAR_PENDING')
          }
          title={
            globalState === 'TRIPPED' || globalState === 'CLEAR_PENDING'
              ? undefined
              : 'Trip the kill switch first — cancel-all is only enabled while new placements are blocked.'
          }
          style={
            globalState === 'TRIPPED' || globalState === 'CLEAR_PENDING'
              ? btnStyle('#7c3aed')
              : { ...btnStyle('#7c3aed'), opacity: 0.5, cursor: 'not-allowed' }
          }
        >
          Cancel ALL Open Orders
        </button>
      </div>

      {cancelResult && (
        <div
          style={{
            marginTop: '0.75rem',
            border: `1px solid ${cancelResult.status === 'ok' ? '#16a34a' : '#f59e0b'}`,
            borderRadius: 6,
            padding: '0.5rem 0.75rem',
            fontSize: '0.875rem',
            backgroundColor: '#fff',
            color: '#374151',
          }}
        >
          <div>
            <strong>Last cancel-all result:</strong> attempted=
            {cancelResult.attempted}, cancelled={cancelResult.cancelled},
            failed={cancelResult.failed}, skipped={cancelResult.skipped},
            raced_filled={cancelResult.raced_filled ?? 0},
            refresh_failures={cancelResult.refresh_failures ?? 0}
          </div>
          {(cancelResult.raced_filled ?? 0) > 0 && (
            <div style={{
              marginTop: '0.25rem',
              padding: '0.25rem 0.5rem',
              borderLeft: '3px solid #f59e0b',
              backgroundColor: '#fffbeb',
              color: '#92400e',
            }}>
              <strong>⚠ {cancelResult.raced_filled} order(s) filled before the cancel landed.</strong>
              {' '}New exposure may need manual flattening — check per-account details below.
            </div>
          )}
          {/* PR #240 round-3 review P2: surface refresh failures
              explicitly so the operator sees that partial status is
              due to broker get_orders() failing — fresh broker orders
              not in the cache may still be live. */}
          {(cancelResult.refresh_failures ?? 0) > 0 && (
            <div style={{
              marginTop: '0.25rem',
              padding: '0.25rem 0.5rem',
              borderLeft: '3px solid #f59e0b',
              backgroundColor: '#fffbeb',
              color: '#92400e',
            }}>
              <strong>⚠ Could not verify broker open-order set for {cancelResult.refresh_failures} account(s).</strong>
              {' '}Fresh broker orders not in the cache may still be live — investigate manually.
            </div>
          )}
          {cancelResult.per_account.length > 0 && (
            <ul style={{ margin: '0.25rem 0 0 1rem', padding: 0 }}>
              {cancelResult.per_account.map((a) => (
                <li key={a.broker_account_id}>
                  {a.broker_account_id}: {a.status} (att={a.attempted},
                  ok={a.cancelled}, fail={a.failed}, skip={a.skipped},
                  raced_filled={a.raced_filled ?? 0}
                  {a.broker_orders_refresh_failed
                    ? ', refresh_failed=true'
                    : ''})
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {confirmDialog && (
        <div
          role="dialog"
          aria-modal="true"
          style={{
            position: 'fixed',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            backgroundColor: 'rgba(0, 0, 0, 0.45)',
            zIndex: 1000,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <div
            style={{
              backgroundColor: '#fff',
              borderRadius: 12,
              padding: '1.5rem',
              maxWidth: 480,
              width: '100%',
              boxShadow: '0 10px 25px rgba(0,0,0,0.15)',
            }}
          >
            <h3 style={{ marginTop: 0, color: '#111827' }}>{confirmDialog.title}</h3>
            <p style={{ color: '#374151', fontSize: '0.875rem' }}>
              {confirmDialog.prompt}
            </p>
            <label
              style={{
                display: 'block',
                fontSize: '0.875rem',
                color: '#374151',
                marginBottom: '0.25rem',
              }}
            >
              {confirmDialog.reasonLabel}
            </label>
            <textarea
              value={reasonInput}
              onChange={(e) => setReasonInput(e.target.value)}
              rows={3}
              style={{
                width: '100%',
                padding: '0.5rem',
                borderRadius: 6,
                border: '1px solid #d1d5db',
                fontFamily: 'inherit',
                fontSize: '0.875rem',
                marginBottom: '0.75rem',
              }}
              autoFocus
            />
            {confirmDialog.hardOption && (
              <label
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '0.5rem',
                  fontSize: '0.875rem',
                  color: '#374151',
                  marginBottom: '0.75rem',
                }}
              >
                <input
                  type="checkbox"
                  checked={hardInput}
                  onChange={(e) => setHardInput(e.target.checked)}
                />
                <span>
                  HARD trip (block exits too) — for operator panic stops only.
                </span>
              </label>
            )}
            {confirmDialog.requireStepUpToken && (
              <div style={{ marginBottom: '0.75rem' }}>
                <label style={{ display: 'block', fontSize: '0.875rem', color: '#374151', marginBottom: '0.25rem' }}>
                  Step-up token (LIVE)
                </label>
                {confirmDialog.stepUpInstructions && (
                  <pre style={{
                    fontSize: '0.75rem',
                    backgroundColor: '#f3f4f6',
                    padding: '0.5rem',
                    borderRadius: 4,
                    overflowX: 'auto',
                    margin: '0 0 0.5rem 0',
                    color: '#374151',
                    whiteSpace: 'pre-wrap',
                  }}>{confirmDialog.stepUpInstructions}</pre>
                )}
                <input
                  type="text"
                  value={stepUpTokenInput}
                  onChange={(e) => setStepUpTokenInput(e.target.value)}
                  placeholder="Paste step-up token_id here"
                  style={{
                    width: '100%',
                    padding: '0.5rem',
                    borderRadius: 6,
                    border: '1px solid #d1d5db',
                    fontFamily: 'monospace',
                    fontSize: '0.875rem',
                  }}
                />
              </div>
            )}
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.5rem' }}>
              <button
                type="button"
                onClick={closeDialog}
                disabled={busy}
                style={btnStyle('#6b7280', 'outline')}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void runDialog()}
                disabled={
                  busy
                  || !reasonInput.trim()
                  || (
                    !!confirmDialog.requireStepUpToken
                    && !stepUpTokenInput.trim()
                  )
                }
                style={btnStyle('#dc2626')}
              >
                {busy ? 'Working…' : 'Confirm'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

const btnStyle = (
  color: string,
  variant: 'solid' | 'outline' = 'solid',
): React.CSSProperties => ({
  padding: '0.5rem 0.875rem',
  borderRadius: 6,
  border: `1px solid ${color}`,
  backgroundColor: variant === 'outline' ? 'transparent' : color,
  color: variant === 'outline' ? color : '#fff',
  fontSize: '0.875rem',
  fontWeight: 600,
  cursor: 'pointer',
});

export default KillSwitchPanel;
