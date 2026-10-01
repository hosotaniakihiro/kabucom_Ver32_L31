import { API_DEFAULTS } from '@ie-miru/config';
import type { Building, CameraPose, LocationFix, LocationPermission } from '@ie-miru/domain';
import { deviceId, load, save } from './storage';

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? '';

export class ApiError extends Error {
  constructor(readonly status: number, readonly code: string) {
    super(code);
  }
}

async function call<T>(path: string, init: RequestInit = {}, timeoutMs = 15000): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const headers: Record<string, string> = { [API_DEFAULTS.deviceHeader]: deviceId(), ...(init.headers as Record<string, string>) };
    if (init.body && !(init.body instanceof FormData)) headers['Content-Type'] = 'application/json';
    const res = await fetch(`${BASE}${path}`, { ...init, headers, signal: ctrl.signal });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new ApiError(res.status, (body as { error?: string }).error ?? `http_${res.status}`);
    return body as T;
  } catch (e) {
    if (e instanceof ApiError) throw e;
    throw new ApiError(0, 'network');
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  health: () => call<{ ok: boolean; modes: Record<string, string>; persistence: boolean }>('/v1/health'),
  candidates: (input: { fix: LocationFix | null; pose: CameraPose | null; permission: LocationPermission; measuredDistanceM?: number | null }) =>
    call<any>('/v1/candidates', { method: 'POST', body: JSON.stringify(input) }),
  nearby: (lat: number, lng: number, radius = 120) => call<{ buildings: Building[]; sources: any[]; status: string }>(`/v1/buildings/nearby?lat=${lat}&lng=${lng}&radius=${radius}`),
  building: (id: string) => call<Building>(`/v1/buildings/${encodeURIComponent(id)}`),
  /** 建物レポート。通信失敗時は前回の結果を返す（offline フラグ付き） */
  async report(id: string, hint?: { lat: number; lng: number }): Promise<any> {
    const q = hint ? `?lat=${hint.lat}&lng=${hint.lng}` : '';
    try {
      const r = await call<any>(`/v1/buildings/${encodeURIComponent(id)}/report${q}`, {}, 25000);
      save(`iemiru.report.${id}`, { at: Date.now(), report: r });
      return r;
    } catch (e) {
      const cached = load<{ at: number; report: any } | null>(`iemiru.report.${id}`, null);
      if (cached && e instanceof ApiError && e.status === 0) return { ...cached.report, offline: { cachedAt: cached.at } };
      throw e;
    }
  },
  call,
};
