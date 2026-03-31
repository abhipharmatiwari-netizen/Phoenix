import React from 'react';

interface Props {
  onClose: () => void;
}

const SHORTCUTS = [
  { keys: 'g → o', description: 'Go to Overview' },
  { keys: 'g → a', description: 'Go to Alerts' },
  { keys: 'g → p', description: 'Go to Positions' },
  { keys: 'g → t', description: 'Go to Trades' },
  { keys: 'g → r', description: 'Go to Orders' },
  { keys: 'g → c', description: 'Go to Control Tower' },
  { keys: 'g → s', description: 'Go to Safety' },
  { keys: '?', description: 'Show this help' },
];

const ShortcutHelp: React.FC<Props> = ({ onClose }) => (
  <div
    style={{
      position: 'fixed', inset: 0, backgroundColor: 'rgba(0,0,0,0.5)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
    }}
    onClick={onClose}
    role="dialog"
    aria-label="Keyboard shortcuts"
  >
    <div
      style={{
        backgroundColor: '#fff', borderRadius: 8, padding: '1.5rem',
        maxWidth: 400, width: '90%', boxShadow: '0 4px 12px rgba(0,0,0,0.15)',
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
        <h3 style={{ margin: 0 }}>Keyboard Shortcuts</h3>
        <button
          onClick={onClose}
          aria-label="Close shortcuts help"
          style={{ border: 'none', background: 'none', cursor: 'pointer', fontSize: '1.25rem', color: '#6b7280' }}
        >
          ✕
        </button>
      </div>
      <table style={{ width: '100%', fontSize: '0.875rem' }}>
        <tbody>
          {SHORTCUTS.map((s) => (
            <tr key={s.keys} style={{ borderBottom: '1px solid #f3f4f6' }}>
              <td style={{ padding: '0.4rem 0' }}>
                <kbd style={{
                  backgroundColor: '#f3f4f6', border: '1px solid #d1d5db', borderRadius: 4,
                  padding: '0.15rem 0.5rem', fontFamily: 'monospace', fontSize: '0.8rem',
                }}>
                  {s.keys}
                </kbd>
              </td>
              <td style={{ padding: '0.4rem 0.5rem', color: '#4b5563' }}>{s.description}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  </div>
);

export default ShortcutHelp;
