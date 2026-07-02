export enum Role {
  VIEWER = 'viewer',
  READONLY = 'readonly',
  OPERATOR = 'operator',
  ADMIN = 'admin',
  BREAK_GLASS_ADMIN = 'break_glass_admin',
}

export interface User {
  id: string;
  email: string;
  role: Role;
  tenantIds?: string[];
  brokerAccountIds?: string[];
  canAccessAllTenants?: boolean;
}

const ROLE_PRIORITY: Record<Role, number> = {
  [Role.VIEWER]: 0,
  [Role.READONLY]: 0,
  [Role.OPERATOR]: 1,
  [Role.ADMIN]: 2,
  [Role.BREAK_GLASS_ADMIN]: 3,
};

export function normalizeRole(value: string | Role | null | undefined): Role {
  const token = String(value || '').trim().toLowerCase();
  switch (token) {
    case Role.ADMIN:
      return Role.ADMIN;
    case Role.BREAK_GLASS_ADMIN:
      return Role.BREAK_GLASS_ADMIN;
    case Role.OPERATOR:
      return Role.OPERATOR;
    case Role.READONLY:
      return Role.READONLY;
    case Role.VIEWER:
    default:
      return Role.VIEWER;
  }
}

export function hasRequiredRole(currentRole: Role, requiredRole: Role): boolean {
  return ROLE_PRIORITY[currentRole] >= ROLE_PRIORITY[requiredRole];
}
