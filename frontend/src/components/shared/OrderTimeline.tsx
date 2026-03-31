import React from 'react';
import { OrderEvent } from '../../types/trading';

interface Props {
  events: OrderEvent[];
}

const STATUS_COLORS: Record<string, string> = {
  submitted: '#3b82f6',
  open: '#3b82f6',
  pending: '#f59e0b',
  acked: '#8b5cf6',
  partial_fill: '#06b6d4',
  filled: '#16a34a',
  complete: '#16a34a',
  cancelled: '#6b7280',
  rejected: '#dc2626',
  failed: '#dc2626',
};

const OrderTimeline: React.FC<Props> = ({ events }) => {
  if (!events || events.length === 0) {
    return <p style={{ color: '#9ca3af', fontSize: '0.8rem', padding: '0.5rem 0' }}>No lifecycle events recorded.</p>;
  }

  return (
    <div style={{ padding: '0.5rem 1rem' }}>
      {events.map((evt, i) => {
        const color = STATUS_COLORS[evt.status.toLowerCase()] || '#6b7280';
        const isLast = i === events.length - 1;
        return (
          <div key={`${evt.status}-${evt.timestamp}-${i}`} style={{ display: 'flex', gap: '0.75rem', minHeight: 40 }}>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', width: 20 }}>
              <div style={{
                width: 12, height: 12, borderRadius: '50%', backgroundColor: color,
                border: '2px solid #fff', boxShadow: `0 0 0 2px ${color}`,
                flexShrink: 0,
              }} />
              {!isLast && <div style={{ width: 2, flex: 1, backgroundColor: '#e5e7eb' }} />}
            </div>
            <div style={{ paddingBottom: '0.5rem' }}>
              <span style={{ fontWeight: 600, fontSize: '0.8rem', color, textTransform: 'uppercase' }}>
                {evt.status}
              </span>
              <span style={{ fontSize: '0.75rem', color: '#9ca3af', marginLeft: '0.5rem' }}>
                {evt.timestamp ? new Date(evt.timestamp).toLocaleString() : ''}
              </span>
              {evt.message && (
                <div style={{ fontSize: '0.75rem', color: '#6b7280', marginTop: 2 }}>{evt.message}</div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
};

export default OrderTimeline;
