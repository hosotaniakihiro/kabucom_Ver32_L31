/**
 * ローカル開発・E2E 用サーバ（Node）。本番は Cloudflare Workers（src/worker.ts）。
 * - API: createApp（Workers と同じコード）
 * - 永続化: node:sqlite による D1 互換 + ファイル R2 互換（IEMIRU_DATA_DIR）
 * - 静的配信: apps/web/dist
 */
import { serve } from '@hono/node-server';
import { serveStatic } from '@hono/node-server/serve-static';
import { Hono } from 'hono';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createApp } from './app';
import { createServices, type Env } from './env';

const here = dirname(fileURLToPath(import.meta.url));
const webDist = resolve(here, '../../web/dist');

function loadDevVars(): Record<string, string> {
  const p = resolve(here, '../.dev.vars');
  if (!existsSync(p)) return {};
  return Object.fromEntries(
    readFileSync(p, 'utf8')
      .split('\n')
      .map((l) => l.trim())
      .filter((l) => l && !l.startsWith('#') && l.includes('='))
      .map((l) => [l.slice(0, l.indexOf('=')).trim(), l.slice(l.indexOf('=') + 1).trim().replace(/^"|"$/g, '')]),
  );
}

export async function startServer(port = Number(process.env.PORT ?? 8787)) {
  const env: Env = { ...loadDevVars(), ...(process.env as Record<string, string>) };
  let repos = null;
  if (process.env.IEMIRU_PERSIST !== '0') {
    const mod = await import('./storage/local').catch(() => null);
    if (mod) repos = await mod.createLocalRepositories(process.env.IEMIRU_DATA_DIR ?? resolve(here, '../.data'));
  }
  const api = createApp({ services: createServices(env), repos });
  const root = new Hono();
  root.route('/', api);
  if (existsSync(webDist)) {
    const rel = relative(process.cwd(), webDist) || '.';
    root.use('/*', serveStatic({ root: rel }));
    root.get('*', (c) => c.html(readFileSync(join(webDist, 'index.html'), 'utf8')));
  }
  const server = serve({ fetch: root.fetch, port, hostname: process.env.HOST ?? '0.0.0.0' });
  console.log(`[ie-miru] http://localhost:${port}  modes=${JSON.stringify(createServices(env).modes)} persistence=${!!repos} web=${existsSync(webDist)}`);
  return server;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  void startServer();
}
