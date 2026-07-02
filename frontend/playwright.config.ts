import { defineConfig } from '@playwright/test';

const externalBaseURL = process.env.PHOENIX_PLAYWRIGHT_BASE_URL;
const useExternalServer = Boolean(externalBaseURL);

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: {
    timeout: 10_000,
  },
  fullyParallel: false,
  reporter: [['list']],
  use: {
    baseURL: externalBaseURL || 'http://127.0.0.1:3000',
    trace: 'retain-on-failure',
  },
  webServer: useExternalServer
    ? undefined
    : {
      command: 'npm run server',
      url: 'http://127.0.0.1:3000',
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
      gracefulShutdown: { signal: 'SIGINT', timeout: 500 },
    },
});
