import { defineConfig, devices } from '@playwright/test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const dataDir = mkdtempSync(join(tmpdir(), 'iemiru-e2e-'));

export default defineConfig({
  testDir: 'tests/e2e',
  timeout: 60_000,
  retries: 0,
  workers: 1,
  reporter: [['list'], ['json', { outputFile: 'test-results/e2e-results.json' }]],
  use: {
    baseURL: 'http://localhost:8799',
    ...devices['iPhone 13'],
    browserName: 'chromium',
    launchOptions: { args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'npx tsx apps/api/src/node-server.ts',
    url: 'http://localhost:8799/v1/health',
    reuseExistingServer: false,
    timeout: 60_000,
    env: { PORT: '8799', DATA_MODE: 'mock', IEMIRU_DATA_DIR: dataDir },
  },
});
