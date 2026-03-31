import React, { createContext, useContext, useState, useEffect } from 'react';
import jwt_decode from 'jwt-decode';
import { User, Role } from '../lib/rbac';

interface DecodedToken {
  sub: string;
  email: string;
  role: Role;
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
      role: decodedToken.role,
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

    const nextUser = decodeToken(token);
    if (nextUser) {
      setUser(nextUser);
      if (typeof window !== 'undefined') {
        window.localStorage.setItem('token', token);
      }
      return;
    }

    setUser(null);
    if (typeof window !== 'undefined') {
      window.localStorage.removeItem('token');
    }
    setToken(null);
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
