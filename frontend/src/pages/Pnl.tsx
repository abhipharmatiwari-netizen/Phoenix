import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { TenantService } from '../client';
import { BrokerAccount, PnLSnapshot } from '../types/trading';
import LoadingSpinner from '../components/shared/LoadingSpinner';
import StaleBanner from '../components/shared/StaleBanner';
import PnlChart from '../components/charts/PnlChart';
import Card from '../components/shared/Card';

const REFRESH_MS = 15_000;
const ALL_STRATEGIES = '__all__';

const Pnl: React.FC = () => {
  const [accounts, setAccounts] = useState<BrokerAccount[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState('');
  const [strategies, setStrategies] = useState<string[]>([]);
  const [selectedStrategy, setSelectedStrategy] = useState<string>(ALL_STRATEGIES);
  const [pnl, setPnl] = useState<PnLSnapshot | null>(null);
  const [strategyUnknown, setStrategyUnknown] = useState(false);
  const [pnlHistory, setPnlHistory] = useState<{ time: string; pnl: number }[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const fetch = async () => {
      try {
        const resp = await TenantService.listMyAccounts();
        if (active) {
          setAccounts(resp.accounts);
          if (resp.accounts.length > 0) {
            setSelectedAccountId(resp.accounts[0].broker_account_id);
          }
        }
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : 'Failed to fetch accounts');
      } finally {
        if (active) setLoading(false);
      }
    };
    fetch();
    return () => { active = false; };
  }, []);

  // Refresh available strategies from backend whenever the account changes.
  useEffect(() => {
    if (!selectedAccountId) {
      setStrategies([]);
      return;
    }
    let active = true;
    TenantService.getAccountStrategies(selectedAccountId)
      .then(resp => {
        if (!active) return;
        setStrategies(resp.strategies);
        // Reset selection to "All" so users always see something on account switch.
        setSelectedStrategy(ALL_STRATEGIES);
        setPnlHistory([]);
      })
      .catch(err => {
        if (active) setError(err instanceof Error ? err.message : 'Failed to fetch strategies');
      });
    return () => { active = false; };
  }, [selectedAccountId]);

  const fetchPnl = useCallback(async () => {
    if (!selectedAccountId) return;
    try {
      const strategyArg = selectedStrategy === ALL_STRATEGIES ? undefined : selectedStrategy;
      const resp = await TenantService.getAccountPnl(selectedAccountId, strategyArg);
      setPnl(resp.pnl);
      setStrategyUnknown(Boolean(resp.strategy_unknown));
      if (resp.pnl) {
        const now = new Date();
        const timeStr = now.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' });
        const totalPnl = (resp.pnl.realized_pnl ?? 0) + (resp.pnl.unrealized_pnl ?? 0);
        setPnlHistory(prev => {
          const next = [...prev, { time: timeStr, pnl: totalPnl }];
          return next.length > 100 ? next.slice(-100) : next;
        });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch PnL');
    }
  }, [selectedAccountId, selectedStrategy]);

  useEffect(() => {
    fetchPnl();
    const timer = setInterval(fetchPnl, REFRESH_MS);
    return () => clearInterval(timer);
  }, [fetchPnl]);

  const totalPnl = (pnl?.realized_pnl ?? 0) + (pnl?.unrealized_pnl ?? 0);
  const drawdown = useMemo(() => {
    if (pnlHistory.length === 0) return [];
    let peak = -Infinity;
    return pnlHistory.map(p => {
      peak = Math.max(peak, p.pnl);
      const dd = peak > 0 ? ((p.pnl - peak) / peak) * 100 : 0;
      return { time: p.time, drawdown: Math.min(0, dd) };
    });
  }, [pnlHistory]);

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: '1rem', marginBottom: '1rem', flexWrap: 'wrap' }}>
        <h1 style={{ margin: 0 }}>PnL</h1>
        {accounts.length > 1 && (
          <select
            value={selectedAccountId}
            onChange={e => {
              setSelectedAccountId(e.target.value);
              setPnlHistory([]);
            }}
            aria-label="Select broker account"
            style={{ padding: '0.4rem 0.75rem', border: '1px solid #d1d5db', borderRadius: 4 }}
          >
            {accounts.map(a => (
              <option key={a.broker_account_id} value={a.broker_account_id}>
                {a.display_name} ({a.trading_mode})
              </option>
            ))}
          </select>
        )}
        <select
          value={selectedStrategy}
          onChange={e => { setSelectedStrategy(e.target.value); setPnlHistory([]); }}
          aria-label="Select strategy"
          style={{ padding: '0.4rem 0.75rem', border: '1px solid #d1d5db', borderRadius: 4 }}
        >
          <option value={ALL_STRATEGIES}>All strategies</option>
          {strategies.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>

      {error && <StaleBanner message={error} variant="danger" />}
      {strategyUnknown && (
        <StaleBanner
          message={`No PnL snapshot recorded yet for strategy "${selectedStrategy}". Showing live unrealized PnL and exposure from open positions; realized will appear once trades complete.`}
          variant="warning"
        />
      )}
      {loading ? <LoadingSpinner /> : (
        <>
          {/* Summary Cards */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '0.75rem', marginBottom: '1.5rem' }}>
            <Card title="Realized PnL">
              <span style={{ fontSize: '1.5rem', fontWeight: 700, color: (pnl?.realized_pnl ?? 0) >= 0 ? '#16a34a' : '#dc2626' }}>
                {formatINR(pnl?.realized_pnl ?? 0)}
              </span>
            </Card>
            <Card title="Unrealized PnL">
              <span style={{ fontSize: '1.5rem', fontWeight: 700, color: (pnl?.unrealized_pnl ?? 0) >= 0 ? '#16a34a' : '#dc2626' }}>
                {formatINR(pnl?.unrealized_pnl ?? 0)}
              </span>
            </Card>
            <Card title="Total PnL">
              <span style={{ fontSize: '1.5rem', fontWeight: 700, color: totalPnl >= 0 ? '#16a34a' : '#dc2626' }}>
                {formatINR(totalPnl)}
              </span>
            </Card>
            <Card title="Gross Exposure">
              <span style={{ fontSize: '1.5rem', fontWeight: 700, color: '#374151' }}>
                {formatINR(pnl?.gross_exposure ?? 0)}
              </span>
            </Card>
          </div>

          {/* PnL Chart */}
          <div style={{ border: '1px solid #e5e7eb', borderRadius: 8, padding: '1rem', marginBottom: '1rem', overflowX: 'auto' }}>
            <h3 style={{ margin: '0 0 0.5rem 0', fontSize: '0.875rem', color: '#6b7280' }}>INTRADAY PNL CURVE</h3>
            <PnlChart data={pnlHistory} width={700} height={280} />
          </div>

          {/* Drawdown */}
          {drawdown.length > 1 && (
            <div style={{ border: '1px solid #e5e7eb', borderRadius: 8, padding: '1rem', overflowX: 'auto' }}>
              <h3 style={{ margin: '0 0 0.5rem 0', fontSize: '0.875rem', color: '#6b7280' }}>DRAWDOWN %</h3>
              <svg width={700} height={120} role="img" aria-label="Drawdown chart">
                {drawdown.map((d, i) => {
                  const x = 70 + (i / Math.max(drawdown.length - 1, 1)) * 590;
                  const barH = Math.abs(d.drawdown) * 1;
                  return (
                    <rect key={i} x={x - 2} y={100 - barH} width={4} height={barH}
                      fill="rgba(220, 38, 38, 0.6)" />
                  );
                })}
                <line x1={70} y1={100} x2={660} y2={100} stroke="#e5e7eb" strokeWidth={1} />
                <text x={60} y={104} textAnchor="end" fontSize={10} fill="#9ca3af">0%</text>
              </svg>
            </div>
          )}

          {/* As-of timestamp */}
          {pnl?.as_of && (
            <p style={{ fontSize: '0.75rem', color: '#9ca3af', marginTop: '0.5rem' }}>
              Last updated: {new Date(pnl.as_of).toLocaleString()}
            </p>
          )}
        </>
      )}
    </div>
  );
};

function formatINR(n: number): string {
  return '₹' + n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export default Pnl;
