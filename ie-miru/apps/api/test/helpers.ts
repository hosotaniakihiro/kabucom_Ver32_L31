import { createApp } from '../src/app';
import { createServices } from '../src/env';
import type { Repositories } from '../src/storage/repositories';

export const NOW = () => new Date('2026-10-01T03:00:00Z');
export const DEMO_CENTER = { lat: 35.6466, lng: 139.6532 };

export function makeApp(opts: { repos?: Repositories | null; env?: Record<string, string> } = {}) {
  const services = createServices({ DATA_MODE: 'mock', ...opts.env }, { now: NOW });
  return createApp({ services, repos: opts.repos ?? null, now: NOW });
}

export const DEVICE = { 'X-IeMiru-Device': 'device-test-0001' };

export async function json<T = any>(res: Response | Promise<Response>): Promise<{ status: number; body: T }> {
  const r = await res;
  return { status: r.status, body: (await r.json()) as T };
}
