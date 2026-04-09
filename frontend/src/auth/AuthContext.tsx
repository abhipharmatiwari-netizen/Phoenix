import React, { createContext, useContext, useState, useEffect } from 'react';
import jwt_decode from 'jwt-decode';
import { AuthService } from '../client';
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
  login: (token: string) => void;
  logout: () => void;
  isAuthenticated: boolean;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

function readStoredToken(): string | null {
  if (typeof window === 'undefined') {
    return null;
  }
  return window.localStorage.getItem('token');
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

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [token, setToken] = useState<string | null>(() => readStoredToken());
  const [user, setUser] = useState<User | null>(() => decodeToken(readStoredToken()));

  useEffect(() => {
    if (!token) {
      setUser(null);
      if (typeof window !== 'undefined') {
        window.localStorage.removeItem('token');
      }
      return;
    }

    const fallbackUser = decodeToken(token);
    if (typeof window !== 'undefined') {
      window.localStorage.setItem('token', token);
    }
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
      .catch(() => {
        if (cancelled) {
          return;
        }
        if (!fallbackUser) {
          setUser(null);
          setToken(null);
          if (typeof window !== 'undefined') {
            window.localStorage.removeItem('token');
          }
        }
      });

    return () => {
      cancelled = true;
    };
  }, [token]);

  const login = (newToken: string) => {
    const nextUser = decodeToken(newToken);
    setToken(newToken);
    setUser(nextUser);
    if (typeof window !== 'undefined') {
      if (nextUser) {
        window.localStorage.setItem('token', newToken);
      } else {
        window.localStorage.removeItem('token');
      }
    }
  };

  const logout = () => {
    setToken(null);
    setUser(null);
    if (typeof window !== 'undefined') {
      window.localStorage.removeItem('token');
    }
  };

  const isAuthenticated = !!user;

  return (
    <AuthContext.Provider value={{ token, user, login, logout, isAuthenticated }}>
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
