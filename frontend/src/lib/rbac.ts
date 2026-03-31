export enum Role {
  READONLY = 'readonly',
  OPERATOR = 'operator',
  ADMIN = 'admin',
}

export interface User {
  id: string;
  email: string;
  role: Role;
}

const ROLE_PRIORITY: Record<Role, number> = {
  [Role.READONLY]: 0,
  [Role.OPERATOR]: 1,
  [Role.ADMIN]: 2,
};

export function hasRequiredRole(currentRole: Role, requiredRole: Role): boolean {
  return ROLE_PRIORITY[currentRole] >= ROLE_PRIORITY[requiredRole];
}
