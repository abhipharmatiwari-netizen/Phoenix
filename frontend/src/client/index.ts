import { MatrixResponse } from '../types/controlTower';
import { AlertsResponse, HealthSummary, MitigationsResponse } from '../types/health';
import type { WebSocketConnectionDescriptor } from '../hooks/useWebSocket';
import {
  AuditEvent,
  BalanceResponse,
  BrokerAccount,
  Order,
  PnLSnapshot,
  Position,
  Subscription,
  Tenant,
  Trade,
} from '../types/trading';

const TENANT_STORAGE_KEY = 'phoenix.tenant_id';
const AUTH_TOKEN_STORAGE_KEY = 'token';
const REFRESH_TOKEN_STORAGE_KEY = 'refresh_token';
const DASHBOARD_WS_SUBPROTOCOL = 'phoenix.dashboard.v1';
const DASHBOARD_WS_TICKET_PROTOCOL_PREFIX = 'phoenix.ticket.';

export const AUTH_SESSION_CHANGED_EVENT = 'phoenix-auth-session-changed';

interface LoginPayload {
  email: string;
  password: string;
  cookie_session?: boolean;
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

interface ListSubscriptionsResponse {
  count: number;
  subscriptions: Subscription[];
}

export interface TenantUpsertPayload {
  tenant_id: string;
  name: string;
  email: string;
  phone?: string | null;
  status: string;
  notes?: string | null;
}

export interface BrokerAccountUpsertPayload {
  broker_account_id: string;
  tenant_id: string;
  broker_type: string;
  display_name: string;
  client_code: string;
  secret_ref: string;
  trading_mode: string;
  enabled: boolean;
  default_strategies: string[];
}

export interface SubscriptionUpsertPayload {
  subscription_id: string;
  tenant_id: string;
  broker_account_id: string;
  mode: string;
  start_at: string;
  end_at: string;
}

interface TenantDeactivateResponse {
  status: string;
  tenant: Tenant;
  disabled_accounts: BrokerAccount[];
  expired_subscriptions: Subscription[];
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

export interface OperatorHealthSummaryResponse {
  summary: HealthSummary;
  source: 'admin' | 'public';
  admin_error?: string;
}

interface StrategiesAdminResponse {
  strategies: unknown;
}

interface StrategySelectionAdminResponse {
  strategy_selection: unknown[];
}

interface InstrumentsAdminResponse {
  instruments: unknown;
}

interface ControlTowerTogglePayload {
  tenant_id: string;
  strategy_id: string;
  enabled: boolean;
  reason?: string;
}

export interface StrategyTogglePayload {
  name: string;
  enabled: boolean;
  reason: string;
  step_up_token?: string | null;
}

export interface BreakGlassFlattenPayload {
  tenant_id: string;
  broker_account_id: string;
  underlying: string;
  expiry: string;
  strike: string;
  option_right: string;
  product_type: string;
  reason: string;
  step_up_token?: string | null;
}

export interface StrategyCandidateDiff {
  current: unknown;
  candidate: unknown;
}

export interface StrategyCandidate {
  candidate_id: string;
  strategy_config_id: string;
  tenant_id: string;
  broker_account_id: string;
  strategy_id: string;
  enabled: boolean;
  status: 'pending' | 'approved' | 'rejected' | 'promoted' | 'superseded' | string;
  params: Record<string, unknown>;
  current_params: Record<string, unknown>;
  param_diff: Record<string, StrategyCandidateDiff>;
  metrics: Record<string, unknown>;
  backtest_window: unknown;
  optimizer_version: string;
  created_at: string;
  reviewed_at: string | null;
  reviewed_by: string | null;
  strategy_updated_at: string | null;
}

export interface StrategyCandidateListResponse {
  count: number;
  candidates: StrategyCandidate[];
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
let inMemoryAuthToken: string | null = null;
let legacyAuthCleanupStarted = false;

function safeLocalStorageGetItem(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeLocalStorageSetItem(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Keep the in-memory session path usable when browser storage is blocked.
  }
}

function safeLocalStorageRemoveItem(key: string): void {
  try {
    window.localStorage.removeItem(key);
  } catch {
    // Keep the in-memory session path usable when browser storage is blocked.
  }
}

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
  const tenantId = safeLocalStorageGetItem(TENANT_STORAGE_KEY);
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
    safeLocalStorageSetItem(TENANT_STORAGE_KEY, normalized);
  } else {
    safeLocalStorageRemoveItem(TENANT_STORAGE_KEY);
  }
}

function dispatchAuthSessionChanged(): void {
  if (typeof window === 'undefined') {
    return;
  }
  window.dispatchEvent(new Event(AUTH_SESSION_CHANGED_EVENT));
}

function purgeLegacyStoredAuthSession(): void {
  if (typeof window === 'undefined') {
    return;
  }
  const legacyToken = safeLocalStorageGetItem(AUTH_TOKEN_STORAGE_KEY);
  const legacyRefreshToken = safeLocalStorageGetItem(REFRESH_TOKEN_STORAGE_KEY);
  safeLocalStorageRemoveItem(AUTH_TOKEN_STORAGE_KEY);
  safeLocalStorageRemoveItem(REFRESH_TOKEN_STORAGE_KEY);
  if ((legacyToken || legacyRefreshToken) && !legacyAuthCleanupStarted) {
    legacyAuthCleanupStarted = true;
    void revokeLegacyStoredSession(legacyToken, legacyRefreshToken);
  }
}

async function revokeLegacyStoredSession(
  legacyToken: string | null,
  legacyRefreshToken: string | null,
): Promise<void> {
  const logoutWithToken = async (token: string): Promise<boolean> => {
    if (!token.trim()) {
      return false;
    }
    try {
      const response = await fetch(buildUrl(BACKEND_BASE_URL, '/auth/logout'), {
        method: 'POST',
        credentials: 'include',
        headers: {
          Accept: 'application/json',
          Authorization: `Bearer ${token.trim()}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({}),
      });
      return response.ok;
    } catch {
      return false;
    }
  };

  if (legacyToken && await logoutWithToken(legacyToken)) {
    return;
  }

  const refreshToken = String(legacyRefreshToken || '').trim();
  if (!refreshToken) {
    return;
  }

  try {
    const response = await fetch(buildUrl(BACKEND_BASE_URL, '/auth/refresh'), {
      method: 'POST',
      credentials: 'include',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ refresh_token: refreshToken, cookie_session: false }),
    });
    const raw = await response.text();
    if (!response.ok) {
      return;
    }
    const payload = raw ? safeJsonParse(raw) : null;
    const refreshedToken = typeof payload === 'object' && payload !== null
      ? String((payload as RefreshResponse).token || '').trim()
      : '';
    if (refreshedToken) {
      await logoutWithToken(refreshedToken);
    }
  } catch {
    // Best-effort legacy session revocation only.
  }
}

export function getStoredAuthToken(): string | null {
  if (typeof window === 'undefined') {
    return null;
  }
  purgeLegacyStoredAuthSession();
  return inMemoryAuthToken;
}

export function storeAuthSession(
  token: string,
  refreshToken?: string | null,
): void {
  if (typeof window === 'undefined') {
    return;
  }

  const normalizedToken = token.trim();
  inMemoryAuthToken = normalizedToken || null;
  purgeLegacyStoredAuthSession();

  dispatchAuthSessionChanged();
}

export function clearAuthSession(): void {
  if (typeof window === 'undefined') {
    return;
  }
  inMemoryAuthToken = null;
  purgeLegacyStoredAuthSession();
  dispatchAuthSessionChanged();
}

export async function restoreAuthSession(): Promise<string | null> {
  return refreshStoredSession();
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
  let response: Response;
  try {
    response = await fetch(buildUrl(BACKEND_BASE_URL, '/auth/refresh'), {
      method: 'POST',
      credentials: 'include',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ cookie_session: true }),
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
    credentials: 'include',
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
    throw new Error(formatErrorDetail(detail, `${response.status} ${response.statusText}`));
  }

  return payload as T;
}

function formatErrorDetail(detail: unknown, fallback: string): string {
  if (typeof detail === 'string') {
    return detail || fallback;
  }
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => formatErrorDetail(item, '')).filter(Boolean);
    return parts.join('; ') || fallback;
  }
  if (detail && typeof detail === 'object') {
    const obj = detail as {
      message?: unknown;
      failures?: unknown;
      next_step?: unknown;
      blocking_reasons?: unknown;
      management_disabled_reason?: unknown;
    };
    const message = typeof obj.message === 'string' ? obj.message : '';
    const nextStep = typeof obj.next_step === 'string' ? obj.next_step : '';
    const failures = Array.isArray(obj.failures)
      ? obj.failures.map((item) => String(item)).filter(Boolean)
      : [];
    const blockingReasons = Array.isArray(obj.blocking_reasons)
      ? obj.blocking_reasons.map((item) => String(item)).filter(Boolean)
      : [];
    const disabledReason = typeof obj.management_disabled_reason === 'string'
      ? obj.management_disabled_reason
      : '';
    const joined = [
      message,
      disabledReason,
      failures.join('; '),
      blockingReasons.join('; '),
      nextStep,
    ].filter(Boolean).join(' ');
    if (joined) {
      return joined;
    }
    try {
      return JSON.stringify(detail);
    } catch {
      return fallback;
    }
  }
  return detail == null ? fallback : String(detail);
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
): Promise<WebSocketConnectionDescriptor> {
  const payload = await request<DashboardWebSocketTicketResponse>({
    path: bffPath('/admin/dashboard/ws-ticket'),
    method: 'POST',
    query: { mode },
    body: {},
  });
  const url = new URL('/ws/dashboard', `${inferDashboardWebSocketBaseUrl()}/`);
  url.searchParams.set('mode', payload.mode || mode);
  return {
    url: url.toString(),
    protocols: [
      DASHBOARD_WS_SUBPROTOCOL,
      `${DASHBOARD_WS_TICKET_PROTOCOL_PREFIX}${payload.ticket}`,
    ],
  };
}

export async function buildDashboardWebSocketUrl(
  mode: 'delta' | 'full' = 'delta',
): Promise<WebSocketConnectionDescriptor> {
  return createDashboardWebSocketUrl(mode);
}

export const AuthService = {
  login(payload: LoginPayload): Promise<LoginResponse> {
    return request<LoginResponse>({
      path: '/auth/login',
      method: 'POST',
      body: { ...payload, cookie_session: true },
    });
  },

  me(): Promise<AuthenticatedUserResponse> {
    return request<AuthenticatedUserResponse>({ path: '/auth/me' });
  },

  logout(): Promise<{ message: string }> {
    return request<{ message: string }>({
      path: '/auth/logout',
      method: 'POST',
      body: {},
    });
  },
};

export const DefaultService = {
  async getOperatorHealthSummary(): Promise<OperatorHealthSummaryResponse> {
    try {
      return {
        summary: await request<HealthSummary>({ path: bffPath('/admin/health/summary') }),
        source: 'admin',
      };
    } catch (err) {
      return {
        summary: await request<HealthSummary>({ path: '/health/summary' }),
        source: 'public',
        admin_error: err instanceof Error ? err.message : String(err || 'Admin health unavailable'),
      };
    }
  },

  async getHealthSummary(): Promise<HealthSummary> {
    try {
      return await request<HealthSummary>({ path: bffPath('/admin/health/summary') });
    } catch {
      return request<HealthSummary>({ path: '/health/summary' });
    }
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

  listSubscriptions(): Promise<ListSubscriptionsResponse> {
    return request<ListSubscriptionsResponse>({
      path: bffPath('/admin/subscriptions'),
    });
  },

  upsertTenant(payload: TenantUpsertPayload): Promise<Tenant> {
    return request<Tenant>({
      path: bffPath('/admin/tenants'),
      method: 'POST',
      body: payload,
    });
  },

  upsertBrokerAccount(payload: BrokerAccountUpsertPayload): Promise<BrokerAccount> {
    return request<BrokerAccount>({
      path: bffPath('/admin/broker-accounts'),
      method: 'POST',
      body: payload,
    });
  },

  upsertSubscription(payload: SubscriptionUpsertPayload): Promise<Subscription> {
    return request<Subscription>({
      path: bffPath('/admin/subscriptions'),
      method: 'POST',
      body: payload,
    });
  },

  deactivateTenant(
    tenantId: string,
    payload: { reason: string; status?: string },
  ): Promise<TenantDeactivateResponse> {
    return request<TenantDeactivateResponse>({
      path: bffPath(`/admin/tenants/${encodeURIComponent(tenantId)}/deactivate`),
      method: 'POST',
      body: payload,
    });
  },

  getReleaseEvidence(): Promise<Record<string, unknown>> {
    return request<Record<string, unknown>>({
      path: bffPath('/admin/release-evidence'),
    });
  },

  getStrategies(): Promise<StrategiesAdminResponse> {
    return request<StrategiesAdminResponse>({
      path: bffPath('/admin/strategies'),
    });
  },

  getStrategySelection(): Promise<StrategySelectionAdminResponse> {
    return request<StrategySelectionAdminResponse>({
      path: bffPath('/admin/strategy-selection'),
    });
  },

  getInstruments(): Promise<InstrumentsAdminResponse> {
    return request<InstrumentsAdminResponse>({
      path: bffPath('/admin/instruments'),
    });
  },

  toggleStrategy(payload: StrategyTogglePayload): Promise<{ name: string; enabled: boolean }> {
    return request<{ name: string; enabled: boolean }>({
      path: bffPath('/admin/strategies/toggle'),
      method: 'POST',
      body: payload,
    });
  },

  breakGlassFlatten(payload: BreakGlassFlattenPayload): Promise<Record<string, unknown>> {
    return request<Record<string, unknown>>({
      path: bffPath('/admin/break-glass/flatten'),
      method: 'POST',
      body: payload,
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

export interface KillSwitchPasswordClearPayload {
  scope: 'GLOBAL' | 'TENANT' | 'ACCOUNT' | 'STRATEGY';
  scope_id: string;
  password: string;
  reason: string;
}

export interface KillSwitchPasswordClearResponse {
  status: 'inactive' | 'partial';
  record_id?: number;
  state: string;
  transitions: string[];
  message?: string;
}

export interface KillSwitchDurableRepairPayload {
  reason: string;
  block_exits?: boolean;
}

export interface KillSwitchDurableRepairResponse {
  status: 'repaired' | 'already_durable_active';
  record_id?: string | number | null;
  state: string;
  block_exits: boolean;
  legacy_snapshot?: Record<string, unknown>;
  divergence_before?: Record<string, unknown>;
  divergence_after?: Record<string, unknown>;
  post_recheck?: Record<string, unknown>;
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

  clearWithPassword(
    payload: KillSwitchPasswordClearPayload,
  ): Promise<KillSwitchPasswordClearResponse> {
    return request<KillSwitchPasswordClearResponse>({
      path: bffPath('/admin/kill-switch/clear-with-password'),
      method: 'POST',
      body: payload,
    });
  },

  repairDurableFromLegacy(
    payload: KillSwitchDurableRepairPayload,
  ): Promise<KillSwitchDurableRepairResponse> {
    return request<KillSwitchDurableRepairResponse>({
      path: bffPath('/admin/kill-switch/repair-durable-from-legacy'),
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

  getControlTowerStatus(): Promise<MatrixResponse> {
    return request<MatrixResponse>({ path: bffPath('/api/control_tower/status') });
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

export const StrategyCandidateService = {
  list(params?: {
    status?: string;
    limit?: number;
  }): Promise<StrategyCandidateListResponse> {
    return request<StrategyCandidateListResponse>({
      path: bffPath('/admin/strategy-candidates'),
      query: params as Record<string, string | number | boolean | undefined>,
    });
  },

  get(candidateId: string): Promise<StrategyCandidate> {
    return request<StrategyCandidate>({
      path: bffPath(`/admin/strategy-candidates/${candidateId}`),
    });
  },

  approve(candidateId: string, reason?: string): Promise<StrategyCandidate> {
    return request<StrategyCandidate>({
      path: bffPath(`/admin/strategy-candidates/${candidateId}/approve`),
      method: 'POST',
      body: { reason: reason || null },
    });
  },

  reject(candidateId: string, reason?: string): Promise<StrategyCandidate> {
    return request<StrategyCandidate>({
      path: bffPath(`/admin/strategy-candidates/${candidateId}/reject`),
      method: 'POST',
      body: { reason: reason || null },
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

  getAccountBalance(
    brokerAccountId: string,
  ): Promise<BalanceResponse> {
    return request<BalanceResponse>({
      path: bffPath(`/tenant/me/accounts/${brokerAccountId}/balance`),
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
