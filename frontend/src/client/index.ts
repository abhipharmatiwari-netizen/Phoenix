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
const AUTH_TOKEN_STORAGE_KEY = 'token';
const REFRESH_TOKEN_STORAGE_KEY = 'refresh_token';

export const AUTH_SESSION_CHANGED_EVENT = 'phoenix-auth-session-changed';

interface LoginPayload {
  email: string;
  password: string;
}

interface LoginResponse {
  token: string;
  refresh_token?: string | null;
  expires_in?: number;
  user: {
    id: string;
    email: string;
    name: string;
    role: string;
  };
}

interface RefreshResponse {
  token: string;
  refresh_token?: string | null;
  expires_in?: number;
}

interface AuthenticatedUserResponse {
  id: string;
  email: string;
  name: string;
  role: string;
  tenant_ids?: string[];
  broker_account_ids?: string[];
  can_access_all_tenants?: boolean;
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
  strategy_id: string | null;
  pnl: PnLSnapshot | null;
  strategy_unknown?: boolean;
}

interface StrategiesResponse {
  tenant_id: string;
  broker_account_id: string;
  strategies: string[];
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

function dispatchAuthSessionChanged(): void {
  if (typeof window === 'undefined') {
    return;
  }
  window.dispatchEvent(new Event(AUTH_SESSION_CHANGED_EVENT));
}

export function getStoredAuthToken(): string | null {
  if (typeof window === 'undefined') {
    return null;
  }
  const token = window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY);
  return token ? token.trim() : null;
}

function readStoredRefreshToken(): string | null {
  if (typeof window === 'undefined') {
    return null;
  }
  const token = window.localStorage.getItem(REFRESH_TOKEN_STORAGE_KEY);
  return token ? token.trim() : null;
}

export function storeAuthSession(
  token: string,
  refreshToken?: string | null,
): void {
  if (typeof window === 'undefined') {
    return;
  }

  const normalizedToken = token.trim();
  if (normalizedToken) {
    window.localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, normalizedToken);
  } else {
    window.localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
  }

  if (refreshToken !== undefined) {
    const normalizedRefresh = String(refreshToken || '').trim();
    if (normalizedRefresh) {
      window.localStorage.setItem(REFRESH_TOKEN_STORAGE_KEY, normalizedRefresh);
    } else {
      window.localStorage.removeItem(REFRESH_TOKEN_STORAGE_KEY);
    }
  }

  dispatchAuthSessionChanged();
}

export function clearAuthSession(): void {
  if (typeof window === 'undefined') {
    return;
  }
  window.localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
  window.localStorage.removeItem(REFRESH_TOKEN_STORAGE_KEY);
  dispatchAuthSessionChanged();
}

function bffPath(path: string): string {
  return `/bff/${path.replace(/^\/+/, '')}`;
}

let refreshInFlight: Promise<string | null> | null = null;

function isAuthPath(path: string): boolean {
  const normalized = `/${path.replace(/^\/+/, '')}`;
  return normalized === '/auth/login'
    || normalized === '/auth/refresh'
    || normalized === '/auth/logout';
}

async function refreshStoredSession(): Promise<string | null> {
  if (!refreshInFlight) {
    refreshInFlight = refreshStoredSessionOnce().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

async function refreshStoredSessionOnce(): Promise<string | null> {
  const refreshToken = readStoredRefreshToken();
  if (!refreshToken) {
    return null;
  }

  let response: Response;
  try {
    response = await fetch(buildUrl(BACKEND_BASE_URL, '/auth/refresh'), {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
  } catch {
    return null;
  }

  const raw = await response.text();
  const payload = raw ? safeJsonParse(raw) : null;
  if (!response.ok) {
    if (response.status === 400 || response.status === 401) {
      clearAuthSession();
    }
    return null;
  }

  const nextToken = typeof payload === 'object' && payload !== null
    ? String((payload as RefreshResponse).token || '').trim()
    : '';
  if (!nextToken) {
    clearAuthSession();
    return null;
  }

  const nextRefreshToken = typeof payload === 'object' && payload !== null
    ? (payload as RefreshResponse).refresh_token
    : undefined;
  storeAuthSession(nextToken, nextRefreshToken ?? null);
  return nextToken;
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
  const storedToken = getStoredAuthToken();
  const attachedStoredToken = Boolean(storedToken && !requestHeaders.has('Authorization'));
  if (storedToken && attachedStoredToken) {
    requestHeaders.set('Authorization', `Bearer ${storedToken}`);
  }
  if (includeTenantHeader && !requestHeaders.has('X-Tenant-Id')) {
    const tenantId = getTenantId();
    if (tenantId) {
      requestHeaders.set('X-Tenant-Id', tenantId);
    }
  }
  if (body !== undefined && !requestHeaders.has('Content-Type')) {
    requestHeaders.set('Content-Type', 'application/json');
  }

  const url = buildUrl(baseUrl, path, query);
  const encodedBody = body === undefined ? undefined : JSON.stringify(body);
  const send = () => fetch(url, {
    method,
    headers: requestHeaders,
    body: encodedBody,
  });

  let response = await send();
  if (response.status === 401 && attachedStoredToken && !isAuthPath(path)) {
    const refreshedToken = await refreshStoredSession();
    if (refreshedToken) {
      requestHeaders.set('Authorization', `Bearer ${refreshedToken}`);
      response = await send();
    }
  }

  const raw = await response.text();
  const payload = raw ? safeJsonParse(raw) : null;

  if (!response.ok) {
    const detail = typeof payload === 'object' && payload !== null
      ? (payload as { detail?: unknown }).detail
      : payload;
    if (response.status === 401 && attachedStoredToken && !isAuthPath(path)) {
      clearAuthSession();
    }
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

  me(): Promise<AuthenticatedUserResponse> {
    return request<AuthenticatedUserResponse>({ path: '/auth/me' });
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

// Issue #238: dashboard-driven kill-switch controls.
//
// The state machine is INACTIVE → TRIPPED → CLEAR_PENDING → CLEARED →
// (rearm) INACTIVE. Trip / request-clear / confirm-clear / rearm are
// idempotent against the durable Postgres-backed KillSwitchManager.
// Cancel-all is a separate destructive bulk action that drains broker-
// side open orders.

export interface KillSwitchRecord {
  id: number;
  scope: 'GLOBAL' | 'TENANT' | 'ACCOUNT' | 'STRATEGY';
  scope_id: string;
  state: 'INACTIVE' | 'TRIPPED' | 'CLEAR_PENDING' | 'CLEARED';
  block_exits: boolean;
  tripped_at: string | null;
  tripped_by: string | null;
  trip_reason: string | null;
  cleared_at: string | null;
  cleared_by: string | null;
  clear_reason: string | null;
  clear_request_id: string | null;
  updated_at: string | null;
}

export interface KillSwitchStateResponse {
  source: string;
  records?: KillSwitchRecord[];
  active_count?: number;
  // PR #240 round-5 review P2: backend falls back to the legacy
  // risk_manager flag when the durable manager is unavailable
  // (``source: "risk_manager"``). In that case neither ``records``
  // nor ``legacy_kill_switch`` is populated — the active signal is
  // here. Frontend must honour it to avoid rendering INACTIVE
  // exactly when the durable path is broken.
  kill_switch_activated?: boolean;
  kill_switch_date?: string | null;
  // PR #240 round-3 review P2: backend now surfaces trade_mode so
  // the dashboard can conditionally require step-up tokens
  // (LIVE-only) instead of unconditionally blocking non-LIVE flows.
  trade_mode?: 'LIVE' | 'PAPER' | 'SHADOW' | string;
  // PR #240 round-4 review P3: backend emits keys ``active`` and
  // ``reason`` here (NOT ``legacy_active``/``legacy_reason`` — those
  // appear under ``divergence``).
  legacy_kill_switch?: {
    active: boolean;
    reason?: string | null;
    publisher_seen?: boolean;
    updated_at?: string | null;
  } | null;
  divergence?: {
    divergent: boolean;
    legacy_active: boolean;
    durable_global_active: boolean | null;
    divergence_age_seconds?: number | null;
    publisher_seen?: boolean;
  } | null;
}

export interface KillSwitchTripPayload {
  scope: 'GLOBAL' | 'TENANT' | 'ACCOUNT' | 'STRATEGY';
  scope_id: string;
  reason: string;
  block_exits?: boolean;
}

export interface KillSwitchTripResponse {
  status: 'tripped' | 'block_exits_upgraded';
  record_id: number;
  state: string;
  block_exits: boolean;
  upgraded_in_place: boolean;
}

export interface KillSwitchClearRequestPayload {
  scope: 'GLOBAL' | 'TENANT' | 'ACCOUNT' | 'STRATEGY';
  scope_id: string;
  reason_code: string;
  break_glass?: boolean;
}

export interface KillSwitchRearmPayload {
  scope: 'GLOBAL' | 'TENANT' | 'ACCOUNT' | 'STRATEGY';
  scope_id: string;
  step_up_token?: string | null;
  // PR #240 round-3 review P2: operator-entered reason persisted
  // in the audit event metadata. Required by the dashboard, but the
  // backend treats it as optional for CLI compatibility.
  reason?: string | null;
}

export interface KillSwitchCancelAllPayload {
  reason: string;
  broker_account_id?: string | null;
}

export interface KillSwitchCancelAllPerAccount {
  broker_account_id: string;
  status: 'ok' | 'partial' | 'no_runner' | 'broker_no_cancel_api' | 'out_of_scope';
  attempted: number;
  cancelled: number;
  failed: number;
  skipped: number;
  // PR #240 round-1 review P2: broker-side fill race during cancel —
  // counted separately from ``cancelled`` because it represents NEW
  // exposure that may need manual flattening.
  raced_filled?: number;
  // PR #240 round-3 review P2: surface broker get_orders refresh
  // failure so the dashboard renders the specific reason a
  // per-account result is partial.
  broker_orders_refresh_failed?: boolean;
  errors: Array<Record<string, unknown>>;
}

export interface KillSwitchCancelAllResponse {
  status: 'ok' | 'partial';
  attempted: number;
  cancelled: number;
  failed: number;
  skipped: number;
  raced_filled?: number;
  // PR #240 round-3 review P2: aggregate count of accounts where
  // ``broker.get_orders()`` failed so the dashboard can show
  // "could not verify broker open-order set" specifically.
  refresh_failures?: number;
  // PR #240 round-5/round-6 review P2: aggregate count of runners
  // that were silently skipped because they were outside the
  // scoped admin's entitlement. When > 0 the partial verdict is
  // specifically due to scope-filter rather than broker failures.
  out_of_scope?: number;
  per_account: KillSwitchCancelAllPerAccount[];
}

export interface StepUpIssueResponse {
  token_id: string;
  expires_at: string;
  action_class: string;
}

export const KillSwitchService = {
  getState(): Promise<KillSwitchStateResponse> {
    return request<KillSwitchStateResponse>({
      path: bffPath('/admin/kill-switch/state'),
    });
  },

  trip(payload: KillSwitchTripPayload): Promise<KillSwitchTripResponse> {
    return request<KillSwitchTripResponse>({
      path: bffPath('/admin/kill-switch/trip'),
      method: 'POST',
      body: payload,
    });
  },

  requestClear(
    payload: KillSwitchClearRequestPayload,
  ): Promise<{ status: string; record_id: number; state: string }> {
    return request({
      path: bffPath('/admin/kill-switch/request-clear'),
      method: 'POST',
      body: payload,
    });
  },

  confirmClear(
    payload: KillSwitchRearmPayload,
  ): Promise<{ status: string; record_id: number; state: string }> {
    return request({
      path: bffPath('/admin/kill-switch/confirm-clear'),
      method: 'POST',
      body: payload,
    });
  },

  rearm(
    payload: KillSwitchRearmPayload,
  ): Promise<{ status: string; record_id: number; state: string }> {
    return request({
      path: bffPath('/admin/kill-switch/rearm'),
      method: 'POST',
      body: payload,
    });
  },

  cancelAll(
    payload: KillSwitchCancelAllPayload,
  ): Promise<KillSwitchCancelAllResponse> {
    return request<KillSwitchCancelAllResponse>({
      path: bffPath('/admin/kill-switch/cancel-all'),
      method: 'POST',
      body: payload,
    });
  },

  issueStepUpToken(
    actionClass: string,
    resourceId = '',
  ): Promise<StepUpIssueResponse> {
    return request<StepUpIssueResponse>({
      path: bffPath('/admin/step-up/issue'),
      method: 'POST',
      body: { action_class: actionClass, resource_id: resourceId },
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
    strategyId?: string,
  ): Promise<PnlResponse> {
    return request<PnlResponse>({
      path: bffPath(`/tenant/me/accounts/${brokerAccountId}/pnl`),
      query: strategyId ? { strategy_id: strategyId } : undefined,
      includeTenantHeader: true,
    });
  },

  getAccountStrategies(
    brokerAccountId: string,
  ): Promise<StrategiesResponse> {
    return request<StrategiesResponse>({
      path: bffPath(`/tenant/me/accounts/${brokerAccountId}/strategies`),
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
