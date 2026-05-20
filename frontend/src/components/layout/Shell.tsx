import React, { useState, useEffect, useMemo } from 'react';
import { Outlet, useNavigate } from 'react-router-dom';
import TopNav from './TopNav';
import SideNav from './SideNav';
import StaleBanner from '../shared/StaleBanner';
import ShortcutHelp from '../shared/ShortcutHelp';
import { DefaultService } from '../../client';
import { useKeyboardShortcut } from '../../hooks/useKeyboardShortcut';

const HEALTH_POLL_MS = 30_000;

const Shell = () => {
  const [systemDegraded, setSystemDegraded] = useState(false);
  const [degradedMessage, setDegradedMessage] = useState('');
  const [showHelp, setShowHelp] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    let active = true;
    const checkHealth = async () => {
      try {
        const health = await DefaultService.getHealthSummary();
        if (active) {
          const readinessReady = health.readiness?.ready ?? health.status === 'ok';
          const degraded = health.status !== 'ok' || !readinessReady;
          setSystemDegraded(degraded);
          setDegradedMessage(
            degraded
              ? `System degraded: ${(health.degraded_reasons || []).join(', ') || health.readiness?.reason || health.status}`
              : ''
          );
        }
      } catch (err) {
        if (active) {
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
        <TopNav />
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
