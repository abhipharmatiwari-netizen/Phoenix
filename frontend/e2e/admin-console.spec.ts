import { expect, Page, test } from '@playwright/test';

const NOW = '2026-07-02T01:00:00.000Z';

function b64url(value: unknown): string {
  return Buffer.from(JSON.stringify(value))
    .toString('base64url');
}

function unsignedJwt(role = 'admin'): string {
  const nowSeconds = Math.floor(Date.now() / 1000);
  return [
    b64url({ alg: 'none', typ: 'JWT' }),
    b64url({
      sub: 'operator-1',
      email: 'operator@example.com',
      role,
      iat: nowSeconds,
      exp: nowSeconds + 3600,
    }),
    'test',
  ].join('.');
}

async function installMockWebSocket(page: Page): Promise<void> {
  await page.addInitScript(() => {
    class MockDashboardWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSING = 2;
      static CLOSED = 3;

      readyState = MockDashboardWebSocket.CONNECTING;
      onopen: ((event: Event) => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;

      constructor(public url: string, public protocols?: string | string[]) {
        window.setTimeout(() => {
          this.readyState = MockDashboardWebSocket.OPEN;
          this.onopen?.(new Event('open'));
          this.onmessage?.(new MessageEvent('message', {
            data: JSON.stringify({
              timestamp: new Date().toISOString(),
              trade_mode: 'LIVE',
              pnl: { realized: 2500, open: 400, invalid_marks_count: 0 },
              strategy_selection: [{ selected_strategies: ['ema20'] }],
              instruments: [{ tick_time: new Date().toISOString() }],
            }),
          }));
        }, 10);
      }

      send() {}

      close() {
        this.readyState = MockDashboardWebSocket.CLOSED;
        this.onclose?.(new CloseEvent('close'));
      }
    }

    Object.defineProperty(window, 'WebSocket', {
      configurable: true,
      value: MockDashboardWebSocket,
    });
  });
}

async function routeConsoleApis(page: Page, counters: { adminSummaryHits: number }): Promise<void> {
  const token = unsignedJwt('admin');
  const healthSummary = {
    timestamp: NOW,
    status: 'ok',
    service: 'phoenix',
    operating_mode: 'HUB_AUTHORITATIVE',
    trade_mode: 'LIVE',
    readiness: { ready: true, reason: null },
    schema_status: 'ok',
    stream_worker_expected: true,
    stream_worker_running: true,
    watchdog_running: true,
    tracked_account_count: 2,
    degraded_reasons: [],
    per_account_staleness: [
      {
        broker_account_id: 'A1',
        last_sync_ts: new Date().toISOString(),
        stale: false,
      },
    ],
    alerts: { firing_count: 0, firing_rules: [] },
  };

  await page.route('**/bff/admin/health/summary', async (route) => {
    const authorization = route.request().headers().authorization || '';
    if (!authorization.startsWith('Bearer ')) {
      await route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Missing Authorization header.' }),
      });
      return;
    }
    counters.adminSummaryHits += 1;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(healthSummary),
    });
  });

  await page.route('**/health/summary', async (route) => {
    if (new URL(route.request().url()).pathname !== '/health/summary') {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        timestamp: NOW,
        status: 'ok',
        ready: true,
        service: 'phoenix',
        operating_mode: 'HUB_AUTHORITATIVE',
        trade_mode: 'LIVE',
        stream_worker_expected: true,
        stream_worker_running: true,
      }),
    });
  });

  await page.route('**/auth/refresh', async (route) => {
    await route.fulfill({
      status: 400,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'refresh_token is required' }),
    });
  });

  await page.route('**/auth/login', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        token,
        refresh_token: null,
        expires_in: 3600,
        user: {
          id: 'operator-1',
          email: 'operator@example.com',
          name: 'Operator',
          role: 'admin',
        },
      }),
    });
  });

  await page.route('**/auth/me', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'operator-1',
        email: 'operator@example.com',
        name: 'Operator',
        role: 'admin',
        tenant_ids: ['tenant-1'],
        broker_account_ids: ['A1'],
        can_access_all_tenants: true,
      }),
    });
  });

  await page.route('**/auth/logout', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ message: 'ok' }),
    });
  });

  await page.route('**/bff/admin/dashboard/ws-ticket**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ticket: 'dashboard-ticket',
        expires_at: '2026-07-02T01:05:00Z',
        ttl_seconds: 300,
        mode: 'delta',
        path: '/ws/dashboard',
      }),
    });
  });

  await page.route('**/bff/admin/strategies', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ strategies: { ema20: true, delta_strangle: false } }),
    });
  });

  await page.route('**/bff/admin/audit**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ events: [], count: 0 }),
    });
  });

  await page.route('**/bff/admin/kill-switch/state', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        source: 'postgres',
        trade_mode: 'LIVE',
        active_count: 0,
        records: [
          {
            id: 1,
            scope: 'GLOBAL',
            scope_id: 'GLOBAL',
            state: 'INACTIVE',
            block_exits: false,
            tripped_at: null,
            tripped_by: null,
            trip_reason: null,
            cleared_at: null,
            cleared_by: null,
            clear_reason: null,
            clear_request_id: null,
            updated_at: NOW,
          },
        ],
        legacy_kill_switch: { active: false, reason: null, publisher_seen: true },
        divergence: {
          divergent: false,
          legacy_active: false,
          durable_global_active: false,
        },
      }),
    });
  });
}

async function loginAsAdmin(page: Page): Promise<void> {
  await page.goto('/login');
  await page.getByLabel('Email').fill('operator@example.com');
  await page.getByLabel('Password').fill('correct horse battery staple');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
}

test.beforeEach(async ({ page }) => {
  await installMockWebSocket(page);
});

test('login page desktop', async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  const counters = { adminSummaryHits: 0 };
  await routeConsoleApis(page, counters);

  await page.goto('/login');

  await expect(page.getByRole('heading', { name: 'Phoenix' })).toBeVisible();
  await expect(page.getByText('Trading operations console')).toBeVisible();
  await expect(page.getByText('LIVE')).toBeVisible();
  await expect(page.locator('body')).not.toContainText('watchdog');
  await expect(page.locator('body')).not.toContainText('tracked_account_count');
  expect(counters.adminSummaryHits).toBe(0);
});

test('login page mobile portrait', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await routeConsoleApis(page, { adminSummaryHits: 0 });

  await page.goto('/login');

  await expect(page.getByRole('heading', { name: 'Phoenix' })).toBeVisible();
  await expect(page.getByLabel('Email')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();
});

test('login page mobile landscape', async ({ page }) => {
  await page.setViewportSize({ width: 844, height: 390 });
  await routeConsoleApis(page, { adminSummaryHits: 0 });

  await page.goto('/login');

  await expect(page.getByRole('heading', { name: 'Phoenix' })).toBeVisible();
  await expect(page.getByLabel('Password')).toBeVisible();
  await expect(page.getByText('LIVE')).toBeVisible();
});

test('overview page mobile uses authenticated admin summary', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const counters = { adminSummaryHits: 0 };
  await routeConsoleApis(page, counters);
  const adminSummary = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/bff/admin/health/summary'
    && response.status() === 200
  ));

  await loginAsAdmin(page);
  await adminSummary;

  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
  await expect(page.getByText('Admin source').first()).toBeVisible();
  await expect(page.getByText('Operator Readiness')).toBeVisible();
  expect(counters.adminSummaryHits).toBeGreaterThan(0);
});

test('safety page mobile uses authenticated admin summary', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const counters = { adminSummaryHits: 0 };
  await routeConsoleApis(page, counters);

  await loginAsAdmin(page);
  const adminSummary = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/bff/admin/health/summary'
    && response.status() === 200
  ));
  await page.getByRole('link', { name: 'Safety' }).click();
  await adminSummary;

  await expect(page.getByRole('heading', { name: 'Safety & Emergency Controls' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Global Kill Switch' })).toBeVisible();
  await expect(page.getByText('INACTIVE').first()).toBeVisible();
  expect(counters.adminSummaryHits).toBeGreaterThan(0);
});
