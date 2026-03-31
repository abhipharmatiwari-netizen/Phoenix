import React from 'react';

const LoadingSpinner: React.FC = () => (
  <div style={{ display: 'flex', justifyContent: 'center', padding: '2rem' }}>
    <div
      aria-label="Loading"
      style={{
        width: 32, height: 32, border: '3px solid #e5e7eb',
        borderTop: '3px solid #3b82f6', borderRadius: '50%',
        animation: 'spin 0.8s linear infinite',
      }}
    />
    <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
  </div>
);

export default LoadingSpinner;
