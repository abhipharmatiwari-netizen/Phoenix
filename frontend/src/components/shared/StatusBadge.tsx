import React from 'react';

type BadgeStatus = 'ok' | 'warning' | 'error' | 'unknown' | 'info';

const COLORS: Record<BadgeStatus, string> = {
  ok: '#22c55e',
  warning: '#eab308',
  error: '#ef4444',
  unknown: '#9ca3af',
  info: '#3b82f6',
};

interface Props {
  status: BadgeStatus;
  label?: string;
}

const StatusBadge: React.FC<Props> = ({ status, label }) => (
  <span style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}>
    <span
      aria-label={`Status: ${status}`}
      style={{
        width: 10, height: 10, borderRadius: '50%',
        backgroundColor: COLORS[status], display: 'inline-block',
      }}
    />
    {label && <span style={{ fontSize: '0.875rem' }}>{label}</span>}
  </span>
);

export default StatusBadge;
