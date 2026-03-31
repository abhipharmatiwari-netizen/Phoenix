import React from 'react';

interface HealthTileProps {
  title: string;
  status: 'green' | 'yellow' | 'red';
  value: string;
}

const HealthTile: React.FC<HealthTileProps> = ({ title, status, value }) => {
  return (
    <div className={`health-tile ${status}`}>
      <h3>{title}</h3>
      <p>{value}</p>
    </div>
  );
};

export default HealthTile;
