import React, { useMemo } from 'react';

interface PnlPoint {
  time: string;
  pnl: number;
}

interface Props {
  data: PnlPoint[];
  width?: number;
  height?: number;
}

const PADDING = { top: 20, right: 40, bottom: 30, left: 70 };

const PnlChart: React.FC<Props> = ({ data, width = 700, height = 300 }) => {
  const chartW = width - PADDING.left - PADDING.right;
  const chartH = height - PADDING.top - PADDING.bottom;

  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const { points, yMin, yMax, yTicks, xLabels, zeroY } = useMemo(() => {
    if (data.length === 0) {
      return { points: [], yMin: 0, yMax: 0, yTicks: [], xLabels: [], zeroY: 0 };
    }

    const pnlValues = data.map(d => d.pnl);
    const rawMin = Math.min(0, ...pnlValues);
    const rawMax = Math.max(0, ...pnlValues);
    const range = rawMax - rawMin || 1;
    const pad = range * 0.1;
    const yMinV = rawMin - pad;
    const yMaxV = rawMax + pad;
    const yRange = yMaxV - yMinV;

    const pts = data.map((d, i) => ({
      x: PADDING.left + (i / Math.max(data.length - 1, 1)) * chartW,
      y: PADDING.top + (1 - (d.pnl - yMinV) / yRange) * chartH,
      pnl: d.pnl,
      time: d.time,
    }));

    const zeroYPos = PADDING.top + (1 - (0 - yMinV) / yRange) * chartH;

    const tickCount = 5;
    const ticks = Array.from({ length: tickCount }, (_, i) => {
      const val = yMinV + (i / (tickCount - 1)) * yRange;
      const y = PADDING.top + (1 - (val - yMinV) / yRange) * chartH;
      return { val, y };
    });

    const labelStep = Math.max(1, Math.floor(data.length / 6));
    const labels = data
      .filter((_, i) => i % labelStep === 0 || i === data.length - 1)
      .map((d, _, arr) => ({
        x: PADDING.left + (data.indexOf(d) / Math.max(data.length - 1, 1)) * chartW,
        label: d.time,
      }));

    return { points: pts, yMin: yMinV, yMax: yMaxV, yTicks: ticks, xLabels: labels, zeroY: zeroYPos };
  }, [data, chartW, chartH]);

  if (data.length === 0) {
    return <p style={{ color: '#9ca3af', textAlign: 'center', padding: '2rem' }}>No PnL data to chart.</p>;
  }

  const linePath = points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.x} ${p.y}`).join(' ');

  // Green fill above zero, red fill below zero
  const greenFill = `M ${points[0].x} ${zeroY} ` +
    points.map(p => `L ${p.x} ${Math.min(p.y, zeroY)}`).join(' ') +
    ` L ${points[points.length - 1].x} ${zeroY} Z`;

  const redFill = `M ${points[0].x} ${zeroY} ` +
    points.map(p => `L ${p.x} ${Math.max(p.y, zeroY)}`).join(' ') +
    ` L ${points[points.length - 1].x} ${zeroY} Z`;

  return (
    <svg width={width} height={height} style={{ fontFamily: 'system-ui, sans-serif' }} role="img" aria-label="PnL chart">
      {/* Grid lines */}
      {yTicks.map((t, i) => (
        <g key={i}>
          <line x1={PADDING.left} y1={t.y} x2={PADDING.left + chartW} y2={t.y}
            stroke="#f3f4f6" strokeWidth={1} />
          <text x={PADDING.left - 8} y={t.y + 4} textAnchor="end" fontSize={10} fill="#9ca3af">
            {formatCompact(t.val)}
          </text>
        </g>
      ))}

      {/* Zero line */}
      <line x1={PADDING.left} y1={zeroY} x2={PADDING.left + chartW} y2={zeroY}
        stroke="#374151" strokeWidth={1} strokeDasharray="4 2" />

      {/* Fill areas */}
      <path d={greenFill} fill="rgba(22, 163, 74, 0.15)" />
      <path d={redFill} fill="rgba(220, 38, 38, 0.15)" />

      {/* Line */}
      <path d={linePath} fill="none" stroke="#3b82f6" strokeWidth={2} />

      {/* X-axis labels */}
      {xLabels.map((l, i) => (
        <text key={i} x={l.x} y={height - 5} textAnchor="middle" fontSize={10} fill="#9ca3af">
          {l.label}
        </text>
      ))}

      {/* Axes */}
      <line x1={PADDING.left} y1={PADDING.top} x2={PADDING.left} y2={PADDING.top + chartH}
        stroke="#e5e7eb" strokeWidth={1} />
      <line x1={PADDING.left} y1={PADDING.top + chartH} x2={PADDING.left + chartW} y2={PADDING.top + chartH}
        stroke="#e5e7eb" strokeWidth={1} />
    </svg>
  );
};

function formatCompact(n: number): string {
  if (Math.abs(n) >= 100000) return (n / 100000).toFixed(1) + 'L';
  if (Math.abs(n) >= 1000) return (n / 1000).toFixed(1) + 'K';
  return n.toFixed(0);
}

export default PnlChart;
