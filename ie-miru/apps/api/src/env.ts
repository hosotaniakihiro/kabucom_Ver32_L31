import {
  CompositeBuildingSource, DemoBuildingSource, FallbackGeocoder, GsiReverseGeocoder, HazardService, LiveReinfolibClient,
  MockGeocoder, MockReinfolibClient, OverpassBuildingSource, PlateauMvtBuildingSource, RealEstateDataService,
  type BuildingSource, type FetchLike, type Geocoder, type ReinfolibClient,
} from '@ie-miru/services';
import type { D1Like, R2Like } from './storage/types';

/** Workers の環境変数・バインディング。Secret は wrangler secret で設定し、コードに書かない。 */
export interface Env {
  DB?: D1Like;
  PHOTOS?: R2Like;
  REINFOLIB_API_KEY?: string;
  PLATEAU_MVT_URL?: string;
  PLATEAU_MVT_LAYER?: string;
  PLATEAU_MVT_ZOOM?: string;
  ENABLE_OSM_FALLBACK?: string;
  /** live / mock。mock は外部へ一切アクセスしない */
  DATA_MODE?: string;
  ALLOWED_ORIGINS?: string;
}

export interface Services {
  buildings: BuildingSource;
  realEstate: RealEstateDataService;
  hazards: HazardService;
  geocoder: Geocoder;
  modes: { buildings: string; realEstate: 'live' | 'mock'; geocoder: 'live' | 'mock' | 'live+mock' };
}

export function createServices(env: Env, opts: { fetch?: FetchLike; now?: () => Date } = {}): Services {
  const forceMock = env.DATA_MODE === 'mock';
  const sources: BuildingSource[] = [];
  const modeNames: string[] = [];
  if (!forceMock && env.PLATEAU_MVT_URL) {
    sources.push(new PlateauMvtBuildingSource({ urlTemplate: env.PLATEAU_MVT_URL, layer: env.PLATEAU_MVT_LAYER || null, zoom: env.PLATEAU_MVT_ZOOM ? Number(env.PLATEAU_MVT_ZOOM) : undefined, fetch: opts.fetch }));
    modeNames.push('plateau');
  }
  if (!forceMock && env.ENABLE_OSM_FALLBACK === '1') {
    sources.push(new OverpassBuildingSource({ fetch: opts.fetch }));
    modeNames.push('osm');
  }
  // 実データの建物ソースが無い場合のみデモ（実データがある場合に勝手にデモへ落とさない）
  if (sources.length === 0) {
    sources.push(new DemoBuildingSource());
    modeNames.push('demo');
  }
  const client: ReinfolibClient =
    !forceMock && env.REINFOLIB_API_KEY ? new LiveReinfolibClient({ apiKey: env.REINFOLIB_API_KEY, fetch: opts.fetch }) : new MockReinfolibClient();
  const geocoder: Geocoder = forceMock
    ? new MockGeocoder()
    : client.mode === 'mock'
      ? new FallbackGeocoder([new GsiReverseGeocoder({ fetch: opts.fetch, timeoutMs: 3000 }), new MockGeocoder()])
      : new GsiReverseGeocoder({ fetch: opts.fetch });
  return {
    buildings: new CompositeBuildingSource(sources),
    realEstate: new RealEstateDataService(client, opts.now),
    hazards: new HazardService(client),
    geocoder,
    modes: { buildings: modeNames.join('+'), realEstate: client.mode, geocoder: forceMock ? 'mock' : client.mode === 'mock' ? 'live+mock' : 'live' },
  };
}
