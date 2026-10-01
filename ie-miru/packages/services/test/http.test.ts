import { describe, expect, it, vi } from 'vitest';
import { redact, request, requestJson, UpstreamError } from '../src';

const noSleep = async () => {};

describe('http request', () => {
  it('retries 5xx then succeeds', async () => {
    let n = 0;
    const fetch = vi.fn(async () => (++n < 3 ? new Response('', { status: 502 }) : new Response('{"ok":1}', { status: 200 })));
    expect(await requestJson('https://x', { fetch, sleep: noSleep })).toEqual({ ok: 1 });
    expect(fetch).toHaveBeenCalledTimes(3);
  });
  it('does not retry auth errors', async () => {
    const fetch = vi.fn(async () => new Response('', { status: 401 }));
    await expect(request('https://x', { fetch, sleep: noSleep })).rejects.toMatchObject({ kind: 'auth' });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it('times out', async () => {
    const fetch = (_: string, init?: RequestInit) =>
      new Promise<Response>((_, rej) => init?.signal?.addEventListener('abort', () => rej(new Error('aborted'))));
    await expect(request('https://x', { fetch, timeoutMs: 10, retries: 0 })).rejects.toMatchObject({ kind: 'timeout' });
  });
  it('404 -> null, invalid JSON -> parse error', async () => {
    expect(await requestJson('https://x', { fetch: async () => new Response('', { status: 404 }) })).toBeNull();
    await expect(requestJson('https://x', { fetch: async () => new Response('<html>', { status: 200 }) })).rejects.toBeInstanceOf(UpstreamError);
  });
  it('redacts keys', () => {
    expect(redact('https://a?b=1&api_key=SECRET')).toBe('https://a?b=1&api_key=***');
  });
});
