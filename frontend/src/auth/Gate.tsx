import React from 'react';
import { useAuth } from './AuthContext';
import { Role } from '../lib/rbac';

interface GateProps {
  requiredRoles: Role[];
  children: React.ReactNode;
}

const Gate: React.FC<GateProps> = ({ requiredRoles, children }) => {
  const { user } = useAuth();

  if (!user || !requiredRoles.includes(user.role)) {
    return null;
  }

  return <>{children}</>;
};

export default Gate;
