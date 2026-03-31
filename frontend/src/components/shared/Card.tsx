import React from 'react';

interface Props {
  title: string;
  children: React.ReactNode;
  onClick?: () => void;
  style?: React.CSSProperties;
}

const Card: React.FC<Props> = ({ title, children, onClick, style }) => (
  <div
    onClick={onClick}
    role={onClick ? 'button' : undefined}
    tabIndex={onClick ? 0 : undefined}
    onKeyDown={onClick ? (e) => { if (e.key === 'Enter') onClick(); } : undefined}
    style={{
      border: '1px solid #e5e7eb', borderRadius: 8, padding: '1rem',
      cursor: onClick ? 'pointer' : 'default',
      ...style,
    }}
  >
    <h3 style={{ margin: '0 0 0.5rem 0', fontSize: '0.875rem', color: '#6b7280', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
      {title}
    </h3>
    {children}
  </div>
);

export default Card;
