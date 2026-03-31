import { MatrixResponse } from '../types/controlTower';
import { AlertsResponse, HealthSummary, MitigationsResponse } from '../types/health';
import {
  AuditEvent,
  BrokerAccount,
  Order,
  PnLSnapshot,
  Position,
  Tenant,
  Trade,
} from '../types/trading';

const TENANT_STORAGE_KEY = 'phoenix.tenant_id';

interface LoginPayload {
  email: string;
  password: string;
}

interface LoginResponse {
  token: string;
  user: {
    id: string;
    email: string;
    name: string;
    role: string;
  };
}

interface ListTenantsResponse {
  count: number;
  tenants: Tenant[];
}

interface ListBrokerAccountsResponse {
  count: number;
  broker_accounts: BrokerAccount[];
}

interface ListAccountsResponse {
  tenant_id: string;
  accounts: BrokerAccount[];
}

interface PositionsResponse {
  tenant_id: string;
  broker_account_id: string;
  positions: Position[];
}

interface OrdersResponse {
  tenant_id: string;
  broker_account_id: string;
  orders: Order[];
}

interface PnlResponse {
  tenant_id: string;
  broker_account_id: string;
  strategy_id: string;
  pnl: PnLSnapshot | null;
}

interface TradesResponse {
  tenant_id: string;
  count: number;
  trades: Trade[];
}

interface DashboardWebSocketTicketResponse {
  ticket: string;
  expires_at: string;
  ttl_seconds: number;
  mode: string;
  path: string;
}

interface ControlTowerTogglePayload {
  tenant_id: string;
  strategy_id: string;
  enabled: boolean;
}

interface RequestOptions {
  path: string;
  method?: 'GET' | 'POST';
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined>;
  headers?: HeadersInit;
  includeTenantHeader?: boolean;
  baseUrl?: string;
}

function normalizeBaseUrl(value: string): string {
  return value.replace(/\/+$/, '');
}

function inferBackendBaseUrl(): string {
  const configured = String(process.env.REACT_APP_BACKEND_URL || '').trim();
  if (configured) {
    return normalizeBaseUrl(configured);
  }
  if (typeof window !== 'undefined') {
    if (window.location.hostname === 'localhost' && window.location.port === '3000') {
      return 'http://localhost:8080';
    }
    return normalizeBaseUrl(window.location.origin);
  }
  return 'http://localhost:8080';
}

const BACKEND_BASE_URL = inferBackendBaseUrl();

function buildUrl(
  baseUrl: string,
  path: string,
  query?: Record<string, string | number | boolean | undefined>,
): string {
  const url = new URL(path.replace(/^\/+/, ''), `${normalizeBaseUrl(baseUrl)}/`);
  Object.entries(query || {}).forEach(([key, value]) => {
    if (value !== undefined) {
      url.searchParams.set(key, String(value));
    }
  });
  return url.toString();
}

function readTenantIdFromStorage(): string | null {
  if (typeof window === 'undefined') {
    return null;
  }
  const tenantId = window.localStorage.getItem(TENANT_STORAGE_KEY);
  return tenantId ? tenantId.trim() : null;
}

export function getTenantId(): string {
  return (
    readTenantIdFromStorage()
    || String(process.env.REACT_APP_TENANT_ID || '').trim()
    || 'tenant-default'
  );
}

export function setTenantId(tenantId: string): void {
  const normalized = tenantId.trim();
  if (typeof window === 'undefined') {
    return;
  }
  if (normalized) {
    window.localStorage.setItem(TENANT_STORAGE_KEY, normalized);
  } else {
    window.localStorage.removeItem(TENANT_STORAGE_KEY);
  }
}

function bffPath(path: string): string {
  return `/bff/${path.replace(/^\/+/, '')}`;
}

async function request<T>({
  path,
  method = 'GET',
  body,
  query,
  headers,
  includeTenantHeader = false,
  baseUrl = BACKEND_BASE_URL,
}: RequestOptions): Promise<T> {
  const requestHeaders = new Headers(headers);
  if (!requestHeaders.has('Accept')) {
    requestHeaders.set('Accept', 'application/json');
  }
  if (includeTenantHeader && !requestHeaders.has('X-Tenant-Id')) {
    requestHeaders.set('X-Tenant-Id', getTenantId());
  }
  if (body !== undefined && !requestHeaders.has('Content-Type')) {
    requestHeaders.set('Content-Type', 'application/json');
  }

  const response = await fetch(buildUrl(baseUrl, path, query), {
    method,
    headers: requestHeaders,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  const raw = await response.text();
  const payload = raw ? safeJsonParse(raw) : null;

  if (!response.ok) {
    const detail = typeof payload === 'object' && payload !== null
      ? (payload as { detail?: unknown }).detail
      : payload;
    throw new Error(String(detail || `${response.status} ${response.statusText}`));
  }

  return payload as T;
}

function safeJsonParse(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

function inferDashboardWebSocketBaseUrl(): string {
  const configured = String(process.env.REACT_APP_DASHBOARD_WS_URL || '').trim();
  const baseUrl = configured || BACKEND_BASE_URL;
  return normalizeBaseUrl(baseUrl).replace(/^http/i, 'ws');
}

export async function createDashboardWebSocketUrl(
  mode: 'delta' | 'full' = 'delta',
): Promise<string> {
  const payload = await request<DashboardWebSocketTicketResponse>({
    path: bffPath('/admin/dashboard/ws-ticket'),
    method: 'POST',
    query: { mode },
    body: {},
  });
  const url = new URL('/ws/dashboard', `${inferDashboardWebSocketBaseUrl()}/`);
  url.searchParams.set('ticket', payload.ticket);
  url.searchParams.set('mode', payload.mode || mode);
  return url.toString();
}

export async function buildDashboardWebSocketUrl(
  mode: 'delta' | 'full' = 'delta',
): Promise<string> {
  return createDashboardWebSocketUrl(mode);
}

export const AuthService = {
  login(payload: LoginPayload): Promise<LoginResponse> {
    return request<LoginResponse>({
      path: '/auth/login',
      method: 'POST',
      body: payload,
    });
  },
};

export const DefaultService = {
  getHealthSummary(): Promise<HealthSummary> {
    return request<HealthSummary>({ path: '/health/summary' });
  },

  getHealthAlerts(): Promise<AlertsResponse> {
    return request<AlertsResponse>({ path: '/health/alerts' });
  },

  getHealthMitigations(): Promise<MitigationsResponse> {
    return request<MitigationsResponse>({ path: '/health/mitigations' });
  },
};

export const AdminService = {
  listTenants(): Promise<ListTenantsResponse> {
    return request<ListTenantsResponse>({ path: bffPath('/admin/tenants') });
  },

  listBrokerAccounts(): Promise<ListBrokerAccountsResponse> {
    return request<ListBrokerAccountsResponse>({
      path: bffPath('/admin/broker-accounts'),
    });
  },
};

export const ControlTowerService = {
  getControlTowerMatrix(): Promise<MatrixResponse> {
    return request<MatrixResponse>({ path: bffPath('/api/control_tower/matrix') });
  },

  toggleControlTower(
    payload: ControlTowerTogglePayload,
  ): Promise<ControlTowerTogglePayload> {
    return request<ControlTowerTogglePayload>({
      path: bffPath('/api/control_tower/toggle'),
      method: 'POST',
      body: payload,
    });
  },
};

export const TenantService = {
  listMyAccounts(): Promise<ListAccountsResponse> {
    return request<ListAccountsResponse>({
      path: bffPath('/tenant/me/accounts'),
      includeTenantHeader: true,
    });
  },

  getAccountPositions(brokerAccountId: string): Promise<PositionsResponse> {
    return request<PositionsResponse>({
      path: bffPath(`/tenant/me/accounts/${brokerAccountId}/positions`),
      includeTenantHeader: true,
    });
  },

  getAccountOrders(brokerAccountId: string): Promise<OrdersResponse> {
    return request<OrdersResponse>({
      path: bffPath(`/tenant/me/accounts/${brokerAccountId}/orders`),
      includeTenantHeader: true,
    });
  },

  getAccountPnl(
    brokerAccountId: string,
    strategyId: string,
  ): Promise<PnlResponse> {
    return request<PnlResponse>({
      path: bffPath(`/tenant/me/accounts/${brokerAccountId}/pnl`),
      query: { strategy_id: strategyId },
      includeTenantHeader: true,
    });
  },

  getMyTrades(params?: {
    broker_account_id?: string;
    from_time?: string;
    to_time?: string;
    limit?: number;
  }): Promise<TradesResponse> {
    return request<TradesResponse>({
      path: bffPath('/tenant/me/trades'),
      query: params as Record<string, string | number | boolean | undefined>,
      includeTenantHeader: true,
    });
  },
};

interface AuditListResponse {
  events: AuditEvent[];
  count: number;
}

export const AuditService = {
  getAuditLog(params?: {
    action?: string;
    resource_type?: string;
    limit?: number;
  }): Promise<AuditListResponse> {
    return request<AuditListResponse>({
      path: bffPath('/admin/audit'),
      query: params as Record<string, string | number | boolean | undefined>,
    });
  },
};
