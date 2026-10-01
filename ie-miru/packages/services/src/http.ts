import { API_DEFAULTS } from '@ie-miru/config';

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export type UpstreamErrorKind = 'timeout' | 'network' | 'http' | 'parse' | 'auth' | 'rate_limited' | 'not_configured';

/** 外部データ取得の失敗。UI では「確認できず」として扱う。 */
export class UpstreamError extends Error {
  constructor(
    readonly kind: UpstreamErrorKind,
    message: string,
    readonly status: number | null = null,
    readonly sourceId: string | null = null,
  ) {
    super(message);
    this.name = 'UpstreamError';
  }
}

export interface RequestOptions {
  fetch?: FetchLike;
  timeoutMs?: number;
  retries?: number;
  headers?: Record<string, string>;
  sourceId?: string;
  signal?: AbortSignal;
  /** テスト用: 再試行の待機関数 */
  sleep?: (ms: number) => Promise<void>;
}

const defaultSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

function isRetryable(e: unknown): boolean {
  return e instanceof UpstreamError && (e.kind === 'timeout' || e.kind === 'network' || e.kind === 'rate_limited' || (e.kind === 'http' && (e.status ?? 0) >= 500));
}

/** タイムアウト・再試行（指数バックオフ）付き GET。 */
export async function request(url: string, opts: RequestOptions = {}): Promise<Response> {
  const f = opts.fetch ?? (globalThis.fetch as FetchLike);
  const retries = opts.retries ?? API_DEFAULTS.upstreamRetries;
  const sleep = opts.sleep ?? defaultSleep;
  let lastErr: unknown;
  for (let attempt = 0; attempt <= retries; attempt++) {
    if (attempt > 0) await sleep(250 * 2 ** (attempt - 1));
    const ctrl = new AbortController();
    const onAbort = () => ctrl.abort();
    opts.signal?.addEventListener('abort', onAbort);
    const timer = setTimeout(() => ctrl.abort(), opts.timeoutMs ?? API_DEFAULTS.upstreamTimeoutMs);
    try {
      let res: Response;
      try {
        res = await f(url, { headers: opts.headers, signal: ctrl.signal });
      } catch (e) {
        if (ctrl.signal.aborted && !opts.signal?.aborted) throw new UpstreamError('timeout', `timeout: ${redact(url)}`, null, opts.sourceId ?? null);
        throw new UpstreamError('network', `network error: ${(e as Error)?.message ?? e}`, null, opts.sourceId ?? null);
      }
      if (res.status === 401 || res.status === 403) throw new UpstreamError('auth', `auth failed (${res.status})`, res.status, opts.sourceId ?? null);
      if (res.status === 429) throw new UpstreamError('rate_limited', 'rate limited', 429, opts.sourceId ?? null);
      if (!res.ok && res.status !== 404 && res.status !== 204) throw new UpstreamError('http', `HTTP ${res.status}`, res.status, opts.sourceId ?? null);
      return res;
    } catch (e) {
      lastErr = e;
      if (!isRetryable(e) || opts.signal?.aborted) throw e;
    } finally {
      clearTimeout(timer);
      opts.signal?.removeEventListener('abort', onAbort);
    }
  }
  throw lastErr;
}

export async function requestJson<T = unknown>(url: string, opts: RequestOptions = {}): Promise<T | null> {
  const res = await request(url, opts);
  if (res.status === 404 || res.status === 204) return null;
  const text = await res.text();
  if (!text.trim()) return null;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new UpstreamError('parse', 'invalid JSON', res.status, opts.sourceId ?? null);
  }
}

export async function requestBinary(url: string, opts: RequestOptions = {}): Promise<Uint8Array | null> {
  const res = await request(url, opts);
  if (res.status === 404 || res.status === 204) return null;
  return new Uint8Array(await res.arrayBuffer());
}

/** ログ・エラー文にAPIキー等が混ざらないようにクエリを伏せる */
export function redact(url: string): string {
  return url.replace(/([?&](?:key|api_key|apikey|subscription-key)=)[^&]+/gi, '$1***');
}

export function fillTemplate(template: string, vars: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_, k: string) => (k in vars ? String(vars[k]) : `{${k}}`));
}
