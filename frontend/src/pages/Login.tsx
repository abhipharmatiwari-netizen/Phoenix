import React, { useEffect, useState } from 'react';
import { useAuth } from '../auth/AuthContext';
import { useLocation, useNavigate } from 'react-router-dom';
import { AuthService, DefaultService } from '../client';

const Login: React.FC = () => {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [mode, setMode] = useState('UNKNOWN');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [offline, setOffline] = useState(
    typeof navigator !== 'undefined' ? !navigator.onLine : false,
  );
  const { isAuthenticated, isLoading, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const sessionExpired = Boolean((location.state as { sessionExpired?: boolean } | null)?.sessionExpired);

  useEffect(() => {
    if (!isLoading && isAuthenticated) {
      navigate('/', { replace: true });
    }
  }, [isAuthenticated, isLoading, navigate]);

  useEffect(() => {
    let active = true;
    DefaultService.getHealthSummary()
      .then((summary) => {
        if (active) {
          setMode(String(summary.trade_mode || summary.operating_mode || 'UNKNOWN').toUpperCase());
        }
      })
      .catch(() => {
        if (active) {
          setMode('UNKNOWN');
        }
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    const onOnline = () => setOffline(false);
    const onOffline = () => setOffline(true);
    window.addEventListener('online', onOnline);
    window.addEventListener('offline', onOffline);
    return () => {
      window.removeEventListener('online', onOnline);
      window.removeEventListener('offline', onOffline);
    };
  }, []);

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const response = await AuthService.login({ email, password });
      login(response.token, response.refresh_token ?? null);
      navigate('/');
    } catch (err) {
      setError('Sign-in failed. Check credentials and operator access.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="login-page">
      <section className="login-panel" aria-label="Phoenix sign in">
        <div className="login-panel__brand">
          <div className="login-panel__mark">P</div>
          <div>
            <h1>Phoenix</h1>
            <p>Trading operations console</p>
          </div>
        </div>
        <div className={`env-badge env-badge--${mode.toLowerCase()}`}>
          {mode}
        </div>

        {sessionExpired && (
          <div className="notice notice--warning" role="status">
            Session expired. Sign in again to continue.
          </div>
        )}
        {offline && (
          <div className="notice notice--blocked" role="status">
            Browser is offline. Sign-in is disabled until connectivity returns.
          </div>
        )}
        {error && (
          <div className="notice notice--blocked" role="alert">
            {error}
          </div>
        )}

        <form className="login-form" onSubmit={handleLogin}>
          <label>
            Email
            <input
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <label>
            Password
            <input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          <button type="submit" disabled={submitting || offline}>
            {submitting ? 'Signing in...' : 'Sign in'}
          </button>
        </form>
      </section>
    </main>
  );
};

export default Login;
