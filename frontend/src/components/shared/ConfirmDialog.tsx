import React, { useState } from 'react';

interface Props {
  title: string;
  message: string;
  confirmText?: string;
  requireTyped?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

const ConfirmDialog: React.FC<Props> = ({ title, message, confirmText = 'Confirm', requireTyped, onConfirm, onCancel }) => {
  const [typed, setTyped] = useState('');
  const canConfirm = !requireTyped || typed === requireTyped;

  return (
    <div style={{
      position: 'fixed', inset: 0, backgroundColor: 'rgba(0,0,0,0.5)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
      padding: 'calc(1rem + env(safe-area-inset-top)) calc(1rem + env(safe-area-inset-right)) calc(1rem + env(safe-area-inset-bottom)) calc(1rem + env(safe-area-inset-left))',
    }}>
      <div style={{
        backgroundColor: '#fff', borderRadius: 8, padding: '1.5rem',
        maxWidth: 420, width: 'min(100%, 420px)',
        maxHeight: 'calc(100dvh - 2rem - env(safe-area-inset-top) - env(safe-area-inset-bottom))',
        overflowY: 'auto',
        boxShadow: '0 4px 12px rgba(0,0,0,0.15)',
      }}>
        <h3 style={{ margin: '0 0 0.5rem 0' }}>{title}</h3>
        <p style={{ color: '#4b5563', fontSize: '0.875rem' }}>{message}</p>
        {requireTyped && (
          <div style={{ margin: '0.75rem 0' }}>
            <label style={{ fontSize: '0.75rem', color: '#6b7280' }}>
              Type "{requireTyped}" to confirm:
            </label>
            <input
              value={typed} onChange={(e) => setTyped(e.target.value)}
              style={{ width: '100%', padding: '0.65rem 0.75rem', border: '1px solid #d1d5db', borderRadius: 4, marginTop: 4 }}
              aria-label={`Type ${requireTyped} to confirm`}
            />
          </div>
        )}
        <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end', marginTop: '1rem' }}>
          <button onClick={onCancel} style={{ padding: '0.55rem 1rem', border: '1px solid #d1d5db', borderRadius: 4, cursor: 'pointer', backgroundColor: '#fff' }}>
            Cancel
          </button>
          <button
            onClick={onConfirm} disabled={!canConfirm}
            style={{
              padding: '0.55rem 1rem', border: 'none', borderRadius: 4, cursor: canConfirm ? 'pointer' : 'not-allowed',
              backgroundColor: canConfirm ? '#ef4444' : '#d1d5db', color: '#fff',
            }}
          >
            {confirmText}
          </button>
        </div>
      </div>
    </div>
  );
};

export default ConfirmDialog;
