import React from 'react';

interface Props {
  message: string;
  variant?: 'warning' | 'danger';
}

const StaleBanner: React.FC<Props> = ({ message, variant = 'warning' }) => {
  const bg = variant === 'danger' ? '#fef2f2' : '#fffbeb';
  const border = variant === 'danger' ? '#ef4444' : '#f59e0b';
  const color = variant === 'danger' ? '#991b1b' : '#92400e';

  return (
    <div
      role="alert"
      style={{
        padding: '0.5rem 1rem', backgroundColor: bg,
        borderLeft: `4px solid ${border}`, color, fontSize: '0.875rem',
        marginBottom: '1rem',
      }}
    >
      {message}
    </div>
  );
};

export default StaleBanner;
