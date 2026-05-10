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
  onConfirm: (reason: string, hard: boolean) => Promise<void>;
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

  const globalState: KillSwitchRecord['state'] = globalRecord?.state || 'INACTIVE';
  const colours = STATE_COLOURS[globalState];

  const closeDialog = () => {
    setConfirmDialog(null);
    setReasonInput('');
    setHardInput(false);
  };

  const runDialog = async () => {
    if (!confirmDialog) return;
    const trimmed = reasonInput.trim();
    if (!trimmed) {
      setError('Reason is required');
      return;
    }
    setBusy(true);
    setError(null);
    setActionFeedback(null);
    try {
      await confirmDialog.onConfirm(trimmed, hardInput);
      await fetchState();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action failed');
    } finally {
      setBusy(false);
      closeDialog();
    }
  };

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
    setConfirmDialog({
      title: 'Confirm CLEAR for GLOBAL kill switch',
      prompt: 'Moves CLEAR_PENDING → CLEARED. After this, rearm to return to INACTIVE.',
      reasonLabel: 'Reason / acknowledgement',
      onConfirm: async () => {
        await KillSwitchService.confirmClear({
          scope: 'GLOBAL',
          scope_id: 'GLOBAL',
        });
        setActionFeedback('Clear confirmed. State now CLEARED. Use Rearm to return to INACTIVE.');
      },
    });
  };

  const onRearm = () => {
    setConfirmDialog({
      title: 'Rearm GLOBAL kill switch',
      prompt: 'Moves CLEARED → INACTIVE. In LIVE mode this requires a step-up token, which is fetched and consumed automatically.',
      reasonLabel: 'Reason / acknowledgement',
      onConfirm: async () => {
        let token: string | null = null;
        try {
          const tok = await KillSwitchService.issueStepUpToken('kill_switch_rearm', 'GLOBAL');
          token = tok.token_id;
        } catch (err) {
          // Step-up only required in LIVE; in PAPER/non-LIVE this may
          // 4xx. Try the rearm without a token in that case.
          token = null;
        }
        await KillSwitchService.rearm({
          scope: 'GLOBAL',
          scope_id: 'GLOBAL',
          step_up_token: token,
        });
        setActionFeedback('Rearmed. State now INACTIVE.');
      },
    });
  };

  const onCancelAll = () => {
    setConfirmDialog({
      title: 'Cancel ALL open broker orders',
      prompt: 'Iterates every registered runner and cancels every non-terminal order via the broker adapter. Idempotent — already-cancelled orders are skipped, not failed.',
      reasonLabel: 'Reason',
      onConfirm: async (reason) => {
        const resp = await KillSwitchService.cancelAll({ reason });
        setCancelResult(resp);
        setActionFeedback(
          `Cancel-all ${resp.status}: attempted=${resp.attempted}, cancelled=${resp.cancelled}, failed=${resp.failed}, skipped=${resp.skipped}.`,
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
            <button
              type="button"
              onClick={onTrip}
              disabled={busy}
              style={btnStyle('#dc2626')}
            >
              Upgrade SOFT → HARD
            </button>
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
        <button
          type="button"
          onClick={onCancelAll}
          disabled={busy}
          style={btnStyle('#7c3aed')}
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
            failed={cancelResult.failed}, skipped={cancelResult.skipped}
          </div>
          {cancelResult.per_account.length > 0 && (
            <ul style={{ margin: '0.25rem 0 0 1rem', padding: 0 }}>
              {cancelResult.per_account.map((a) => (
                <li key={a.broker_account_id}>
                  {a.broker_account_id}: {a.status} (att={a.attempted},
                  ok={a.cancelled}, fail={a.failed}, skip={a.skipped})
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
                disabled={busy || !reasonInput.trim()}
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
