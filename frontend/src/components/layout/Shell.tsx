import React, { useState, useEffect, useMemo } from 'react';
import { Outlet, useNavigate } from 'react-router-dom';
import TopNav from './TopNav';
import SideNav from './SideNav';
import StaleBanner from '../shared/StaleBanner';
import ShortcutHelp from '../shared/ShortcutHelp';
import { DefaultService, OperatorHealthSummaryResponse } from '../../client';
import { useKeyboardShortcut } from '../../hooks/useKeyboardShortcut';
import { classifyOperatorHealth, healthReasons } from '../../lib/consoleUtils';

const HEALTH_POLL_MS = 30_000;

const Shell = () => {
  const [systemDegraded, setSystemDegraded] = useState(false);
  const [degradedMessage, setDegradedMessage] = useState('');
  const [healthEnvelope, setHealthEnvelope] = useState<OperatorHealthSummaryResponse | null>(null);
  const [showHelp, setShowHelp] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    let active = true;
    const checkHealth = async () => {
      try {
        const envelope = await DefaultService.getOperatorHealthSummary();
        const health = envelope.summary;
        if (active) {
          const verdict = classifyOperatorHealth(health, envelope.source);
          const degraded = verdict !== 'healthy';
          setHealthEnvelope(envelope);
          setSystemDegraded(degraded);
          setDegradedMessage(
            degraded
              ? [
                envelope.source === 'public'
                  ? 'Operator diagnostics unavailable; showing public reachability only.'
                  : 'System is not confirmed healthy.',
                ...healthReasons(health, envelope.admin_error),
              ].filter(Boolean).join(' ')
              : ''
          );
        }
      } catch (err) {
        if (active) {
          setHealthEnvelope(null);
          setSystemDegraded(true);
          setDegradedMessage(
            `System status unavailable: ${err instanceof Error ? err.message : 'health check failed'}`
          );
        }
      }
    };
    checkHealth();
    const timer = setInterval(checkHealth, HEALTH_POLL_MS);
    return () => { active = false; clearInterval(timer); };
  }, []);

  const shortcuts = useMemo(() => [
    { keys: ['g', 'o'], handler: () => navigate('/') },
    { keys: ['g', 'a'], handler: () => navigate('/alerts') },
    { keys: ['g', 'p'], handler: () => navigate('/positions') },
    { keys: ['g', 't'], handler: () => navigate('/trades') },
    { keys: ['g', 'r'], handler: () => navigate('/orders') },
    { keys: ['g', 'c'], handler: () => navigate('/control-tower') },
    { keys: ['g', 's'], handler: () => navigate('/safety') },
    { keys: ['?'], handler: () => setShowHelp(true) },
  ], [navigate]);

  useKeyboardShortcut(shortcuts);

  return (
    <div className="app-shell">
        <a href="#main-content" className="skip-to-content" 
           onFocus={(e) => { (e.target as HTMLElement).style.top = '0'; }}
           onBlur={(e) => { (e.target as HTMLElement).style.top = '-40px'; }}>
          Skip to content
        </a>
      <SideNav />
      <div className="main-content">
        <TopNav
          mode={healthEnvelope?.summary.trade_mode || healthEnvelope?.summary.operating_mode || 'UNKNOWN'}
          diagnosticSource={healthEnvelope?.source || 'unavailable'}
          diagnosticStatus={healthEnvelope ? classifyOperatorHealth(healthEnvelope.summary, healthEnvelope.source) : 'unknown'}
        />
        {systemDegraded && (
          <StaleBanner message={degradedMessage} variant="danger" />
        )}
        <main id="main-content" className="content" role="main" aria-live="polite">
          <Outlet />
        </main>
      </div>
      {showHelp && <ShortcutHelp onClose={() => setShowHelp(false)} />}
    </div>
  );
};

export default Shell;
