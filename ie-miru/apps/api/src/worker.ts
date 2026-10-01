import { createApp } from './app';
import { createServices, type Env } from './env';

/** Cloudflare Workers エントリ。isolate ごとにアプリを1度だけ組み立てる。 */
let cached: { key: string; app: ReturnType<typeof createApp> } | null = null;

export default {
  async fetch(req: Request, env: Env, ctx: unknown): Promise<Response> {
    const key = [env.DATA_MODE, !!env.REINFOLIB_API_KEY, env.PLATEAU_MVT_URL, env.ENABLE_OSM_FALLBACK].join('|');
    if (!cached || cached.key !== key) {
      const { createRepositories } = await import('./storage/d1');
      cached = {
        key,
        app: createApp({
          services: createServices(env),
          repos: env.DB ? createRepositories(env.DB, env.PHOTOS ?? null) : null,
          allowedOrigins: env.ALLOWED_ORIGINS ? env.ALLOWED_ORIGINS.split(',').map((s) => s.trim()) : [],
        }),
      };
    }
    return cached.app.fetch(req, env, ctx as never);
  },
};
