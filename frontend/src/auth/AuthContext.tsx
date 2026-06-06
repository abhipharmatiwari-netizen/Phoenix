import React, { createContext, useContext, useState, useEffect } from 'react';
import jwt_decode from 'jwt-decode';
import {
  AUTH_SESSION_CHANGED_EVENT,
  AuthService,
  clearAuthSession,
  getStoredAuthToken,
  restoreAuthSession,
  storeAuthSession,
} from '../client';
import { User, Role, normalizeRole } from '../lib/rbac';

interface DecodedToken {
  sub: string;
  email: string;
  role: Role | string;
  exp: number;
}

interface AuthContextType {
  token: string | null;
  user: User | null;
  login: (token: string, refreshToken?: string | null) => void;
  logout: () => void;
  isAuthenticated: boolean;
  isLoading: boolean;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

function readStoredToken(): string | null {
  return getStoredAuthToken();
}

function decodeToken(token: string | null): User | null {
  if (!token) {
    return null;
  }
  try {
    const decodedToken: DecodedToken = jwt_decode(token);
    if (decodedToken.exp * 1000 <= Date.now()) {
      return null;
    }
    return {
      id: decodedToken.sub,
      email: decodedToken.email,
      role: normalizeRole(decodedToken.role),
    };
  } catch {
    return null;
  }
}

function isAuthError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error || '');
  return /invalid token|unauthorized|invalid or expired refresh token/i.test(message);
}

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [token, setToken] = useState<string | null>(() => readStoredToken());
  const [user, setUser] = useState<User | null>(() => decodeToken(readStoredToken()));
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    if (typeof window === 'undefined') {
      return undefined;
    }

    const syncSession = () => {
      setToken(readStoredToken());
    };
    const syncStorageSession = (event: StorageEvent) => {
      if (event.key === 'token' || event.key === 'refresh_token') {
        syncSession();
      }
    };

    window.addEventListener(AUTH_SESSION_CHANGED_EVENT, syncSession);
    window.addEventListener('storage', syncStorageSession);
    return () => {
      window.removeEventListener(AUTH_SESSION_CHANGED_EVENT, syncSession);
      window.removeEventListener('storage', syncStorageSession);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const currentToken = readStoredToken();
    if (currentToken) {
      setIsLoading(false);
      return () => {
        cancelled = true;
      };
    }
    restoreAuthSession()
      .then((restoredToken) => {
        if (cancelled || !restoredToken) {
          return;
        }
        setToken(restoredToken);
        setUser(decodeToken(restoredToken));
      })
      .catch(() => {
        clearAuthSession();
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!token) {
      setUser(null);
      clearAuthSession();
      return;
    }

    const fallbackUser = decodeToken(token);
    storeAuthSession(token);
    setUser(fallbackUser);

    let cancelled = false;
    AuthService.me()
      .then((session) => {
        if (cancelled) {
          return;
        }
        setUser({
          id: session.id,
          email: session.email,
          role: normalizeRole(session.role),
          tenantIds: session.tenant_ids || [],
          brokerAccountIds: session.broker_account_ids || [],
          canAccessAllTenants: !!session.can_access_all_tenants,
        });
      })
      .catch((err) => {
        if (cancelled) {
          return;
        }
        if (!fallbackUser || isAuthError(err)) {
          setUser(null);
          setToken(null);
          clearAuthSession();
        }
      });

    return () => {
      cancelled = true;
    };
  }, [token]);

  const login = (newToken: string, refreshToken?: string | null) => {
    const nextUser = decodeToken(newToken);
    setToken(nextUser ? newToken : null);
    setUser(nextUser);
    if (nextUser) {
      storeAuthSession(newToken, refreshToken ?? null);
    } else {
      clearAuthSession();
    }
  };

  const logout = () => {
    void AuthService.logout().catch(() => undefined);
    setToken(null);
    setUser(null);
    clearAuthSession();
  };

  const isAuthenticated = !!user;

  return (
    <AuthContext.Provider value={{ token, user, login, logout, isAuthenticated, isLoading }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};
