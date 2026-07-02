import { expect, test } from '@playwright/test';

test('production bundle honors nginx CSP', async ({ page }) => {
  test.skip(
    process.env.PHOENIX_CSP_EXPECTED !== '1',
    'Set PHOENIX_PLAYWRIGHT_BASE_URL to an nginx-served production build and PHOENIX_CSP_EXPECTED=1.',
  );

  const cspErrors: string[] = [];
  page.on('console', (message) => {
    const text = message.text();
    if (
      message.type() === 'error'
      && /content security policy|violates.*policy|refused to/i.test(text)
    ) {
      cspErrors.push(text);
    }
  });
  page.on('pageerror', (error) => {
    if (/content security policy|violates.*policy|refused to/i.test(error.message)) {
      cspErrors.push(error.message);
    }
  });

  const response = await page.goto('/');
  expect(response?.status()).toBe(200);
  await expect(page.getByRole('heading', { name: 'Phoenix' })).toBeVisible();
  await expect(page.getByText('Trading operations console')).toBeVisible();
  expect(cspErrors).toEqual([]);
});
