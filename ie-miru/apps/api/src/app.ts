import { Hono } from 'hono';
import { cors } from 'hono/cors';
import { API_DEFAULTS, BRAND, LOCATION_DEFAULTS } from '@ie-miru/config';
import {
  assessLocation, isValidLatLng, parseCameraPose, parseLocationFix, rankCandidates,
  type Building, type LatLng, type LocationPermission,
} from '@ie-miru/domain';
import { buildReport, type BuildingReport } from '@ie-miru/services';
import { TtlCache } from './cache';
import type { Services } from './env';
import type { Repositories } from './storage/repositories';
import { registerUserRoutes } from './routes/user';

export interface AppDeps {
  services: Services;
  repos?: Repositories | null;
  allowedOrigins?: string[];
  now?: () => Date;
}

export type AppEnv = { Variables: { deviceId: string | null } };

const PERMISSIONS: LocationPermission[] = ['granted', 'denied', 'restricted', 'not_determined', 'unsupported'];

function num(v: string | undefined): number | null {
  if (v == null || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export function createApp(deps: AppDeps) {
  const app = new Hono<AppEnv>();
  const reportCache = new TtlCache<BuildingReport>(300, 60 * 60 * 1000);
  const buildingCache = new TtlCache<Building>(2000, 6 * 60 * 60 * 1000);
  const { services } = deps;

  app.use(
    '*',
    cors({
      origin: (origin) => (!deps.allowedOrigins || deps.allowedOrigins.length === 0 || deps.allowedOrigins.includes(origin) ? origin : null),
      allowHeaders: ['Content-Type', API_DEFAULTS.deviceHeader],
      allowMethods: ['GET', 'POST', 'PATCH', 'DELETE', 'OPTIONS'],
    }),
  );
  app.use('*', async (c, next) => {
    const id = c.req.header(API_DEFAULTS.deviceHeader) ?? null;
    c.set('deviceId', id && /^[A-Za-z0-9-]{8,64}$/.test(id) ? id : null);
    await next();
  });

  app.onError((err, c) => {
    console.error('[ie-miru] unhandled', err);
    return c.json({ error: 'internal_error' }, 500);
  });

  app.get('/v1/health', (c) =>
    c.json({ ok: true, name: BRAND.codeName, displayName: BRAND.displayName, modes: services.modes, persistence: !!deps.repos }),
  );

  const remember = (list: Building[]) => list.forEach((b) => buildingCache.set(b.id, b));

  async function findBuilding(id: string, hint: LatLng | null): Promise<Building | null> {
    const cached = buildingCache.get(id);
    if (cached) return cached;
    const b = await services.buildings.getById(id, hint ?? undefined);
    if (b) buildingCache.set(b.id, b);
    return b;
  }

  /** 建物候補（見る） */
  app.post('/v1/candidates', async (c) => {
    const body = (await c.req.json().catch(() => null)) as Record<string, unknown> | null;
    if (!body) return c.json({ error: 'invalid_json' }, 400);
    const permission = PERMISSIONS.includes(body.permission as LocationPermission) ? (body.permission as LocationPermission) : 'granted';
    const fix = parseLocationFix(body.fix);
    const pose = parseCameraPose(body.pose);
    const now = (deps.now?.() ?? new Date()).getTime();
    const assessment = assessLocation(fix, permission, now);
    const position = assessment.position;
    if (!position) {
      return c.json({ assessment, selection: null, sources: [], buildingStatus: 'skipped' });
    }
    const q = await services.buildings.findNear(position, LOCATION_DEFAULTS.candidateRadiusM + 50);
    remember(q.buildings);
    const selection = rankCandidates({
      position,
      accuracyM: assessment.accuracyM,
      // 位置精度が悪いときは方位を使っても誤選択しやすいため距離順・手動選択にする
      pose: assessment.usable ? pose : null,
      buildings: q.buildings,
      measuredDistanceM: typeof body.measuredDistanceM === 'number' ? body.measuredDistanceM : null,
    });
    if (!assessment.usable) selection.message = assessment.message;
    return c.json({ assessment, selection, sources: q.sources, buildingStatus: q.status, buildingError: q.error ?? null });
  });

  /** 地図・手動選択用 */
  app.get('/v1/buildings/nearby', async (c) => {
    const p = { lat: num(c.req.query('lat')) ?? NaN, lng: num(c.req.query('lng')) ?? NaN };
    if (!isValidLatLng(p)) return c.json({ error: 'invalid_location' }, 400);
    const radius = Math.min(300, Math.max(10, num(c.req.query('radius')) ?? 120));
    const q = await services.buildings.findNear(p, radius);
    remember(q.buildings);
    return c.json({ buildings: q.buildings, sources: q.sources, status: q.status, error: q.error ?? null });
  });

  const hintOf = (c: { req: { query(k: string): string | undefined } }): LatLng | null => {
    const p = { lat: num(c.req.query('lat')) ?? NaN, lng: num(c.req.query('lng')) ?? NaN };
    return isValidLatLng(p) ? p : null;
  };

  app.get('/v1/buildings/:id{.+}/report', async (c) => {
    const id = decodeURIComponent(c.req.param('id'));
    const b = await findBuilding(id, hintOf(c));
    if (!b) return c.json({ error: 'building_not_found' }, 404);
    const fresh = c.req.query('fresh') === '1';
    let report = fresh ? undefined : reportCache.get(id);
    if (!report) {
      report = await buildReport(b, { realEstate: services.realEstate, hazards: services.hazards, geocoder: services.geocoder, now: deps.now });
      reportCache.set(id, report);
      if (deps.repos) await deps.repos.analysis.saveReport(report).catch((e) => console.warn('[ie-miru] saveReport failed', e));
    }
    return c.json(report);
  });

  app.get('/v1/buildings/:id{.+}', async (c) => {
    const id = decodeURIComponent(c.req.param('id'));
    const b = await findBuilding(id, hintOf(c));
    return b ? c.json(b) : c.json({ error: 'building_not_found' }, 404);
  });

  registerUserRoutes(app, { ...deps, findBuilding });

  app.notFound((c) => c.json({ error: 'not_found' }, 404));
  return app;
}
