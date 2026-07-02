import React from 'react';
import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { useAuth } from './AuthContext';

const ProtectedRoute: React.FC = () => {
  const { isAuthenticated, isLoading } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return (
      <main className="auth-state-screen">
        <div className="spinner" aria-hidden="true" />
        <h1>Checking Phoenix session</h1>
        <p>Operator access is being verified.</p>
      </main>
    );
  }

  if (!isAuthenticated) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ sessionExpired: true, from: location.pathname }}
      />
    );
  }

  return <Outlet />;
};

export default ProtectedRoute;
