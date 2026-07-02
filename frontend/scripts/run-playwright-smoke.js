const http = require('http');
const path = require('path');
const { spawn, spawnSync } = require('child_process');
const { existsSync } = require('fs');

const isWindows = process.platform === 'win32';
const root = path.resolve(__dirname, '..');
const serveBin = path.join(root, 'node_modules', 'serve', 'build', 'main.js');
const playwrightBin = path.join(root, 'node_modules', 'playwright', 'cli.js');
const port = process.env.PHOENIX_E2E_PORT || '3000';
const baseURL = process.env.PHOENIX_PLAYWRIGHT_BASE_URL || `http://127.0.0.1:${port}`;
const externalServer = Boolean(process.env.PHOENIX_PLAYWRIGHT_BASE_URL);
const extraArgs = process.argv.slice(2);

function waitFor(url, timeoutMs = 30000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const probe = () => {
      const request = http.get(url, (response) => {
        response.resume();
        resolve();
      });
      request.on('error', () => {
        if (Date.now() - started > timeoutMs) {
          reject(new Error(`Timed out waiting for ${url}`));
          return;
        }
        setTimeout(probe, 250);
      });
      request.setTimeout(2000, () => {
        request.destroy();
      });
    };
    probe();
  });
}

function stopProcessTree(child) {
  if (!child || !child.pid) {
    return;
  }
  if (isWindows) {
    spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
    return;
  }
  child.kill('SIGTERM');
}

async function main() {
  if (!existsSync(path.join(root, 'build', 'index.html')) && !externalServer) {
    throw new Error('frontend/build/index.html is missing. Run npm run build before npm run test:e2e.');
  }

  let server = null;
  if (!externalServer) {
    server = spawn(
      process.execPath,
      [serveBin, '-s', 'build', '-l', port],
      {
        cwd: root,
        stdio: 'inherit',
        env: { ...process.env, NO_UPDATE_NOTIFIER: '1' },
      },
    );
    server.on('exit', (code) => {
      if (code !== null && code !== 0) {
        console.error(`serve exited early with code ${code}`);
      }
    });
    await waitFor(baseURL);
  }

  const child = spawn(
    process.execPath,
    [playwrightBin, 'test', ...extraArgs],
    {
      cwd: root,
      stdio: 'inherit',
      env: { ...process.env, PHOENIX_PLAYWRIGHT_BASE_URL: baseURL },
    },
  );

  const code = await new Promise((resolve) => {
    child.on('exit', (exitCode) => resolve(exitCode || 0));
  });

  stopProcessTree(server);
  process.exit(code);
}

main().catch((error) => {
  console.error(error.message || error);
  process.exit(1);
});
