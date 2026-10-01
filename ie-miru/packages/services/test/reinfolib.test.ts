import { describe, expect, it, vi } from 'vitest';
import {
  LiveReinfolibClient, MockReinfolibClient, RealEstateDataService, GsiReverseGeocoder,
  parseLandPrices, parseTransaction, parseTransactions, parseZoningAt, tileBbox, type GeoJsonFC,
} from '../src';
import { lngLatToTile } from '@ie-miru/domain';
import xit001 from './fixtures/xit001.json';
import xpt002 from './fixtures/xpt002.json';
import xkt002 from './fixtures/xkt002.json';

const P = { lat: 35.6466, lng: 139.6532 };
const NOW = () => new Date('2026-10-01T00:00:00Z');

describe('LiveReinfolibClient', () => {
  it('refuses to start without an API key', () => {
    expect(() => new LiveReinfolibClient({ apiKey: '' })).toThrowError(/REINFOLIB_API_KEY/);
  });
  it('sends key in header, never in URL', async () => {
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      expect(url).toBe('https://www.reinfolib.mlit.go.jp/ex-api/external/XIT001?year=2025&city=13112&priceClassification=01');
      expect(url).not.toContain('SECRET');
      expect((init!.headers as Record<string, string>)['Ocp-Apim-Subscription-Key']).toBe('SECRET');
      return new Response(JSON.stringify(xit001), { status: 200 });
    });
    const c = new LiveReinfolibClient({ apiKey: 'SECRET', fetch });
    const rows = await c.transactions({ year: 2025, city: '13112', priceClassification: '01' });
    expect(rows.length).toBe(xit001.data.length);
  });
  it('tile URL uses geojson format', async () => {
    const fetch = vi.fn(async (url: string) => {
      expect(url).toMatch(/XKT002\?response_format=geojson&z=15&x=\d+&y=\d+$/);
      return new Response(JSON.stringify(xkt002), { status: 200 });
    });
    const fc = await new LiveReinfolibClient({ apiKey: 'k', fetch }).tile('XKT002', lngLatToTile(P, 15));
    expect(fc.features.length).toBe(1);
  });
  it('auth failure surfaces as auth error', async () => {
    const c = new LiveReinfolibClient({ apiKey: 'bad', fetch: async () => new Response('', { status: 401 }) });
    await expect(c.transactions({ year: 2025, city: '13112' })).rejects.toMatchObject({ kind: 'auth' });
  });
});

describe('XIT001 parser', () => {
  it('parses spec-shaped rows', () => {
    const tx = parseTransactions(xit001.data as Record<string, unknown>[]);
    expect(tx.length).toBe(3); // 価格なし行は捨てる
    const house = tx[0]!;
    expect(house).toMatchObject({ type: '宅地(土地と建物)', priceYen: 52_000_000, areaM2: 110, totalFloorAreaM2: 95, buildingYear: 1990, year: 2025, quarter: 2, coverageRatioPct: 60, floorAreaRatioPct: 150, priceCategory: 'transaction', unitPriceYenPerM2: null });
    const land = tx[1]!;
    expect(land).toMatchObject({ type: '宅地(土地)', unitPriceYenPerM2: 450_000, buildingYear: null, priceCategory: 'contract' });
    expect(tx[2]).toMatchObject({ areaM2: 2000, areaIsLowerBound: true, buildingYear: 1945 });
  });
  it('missing values are null, not 0', () => {
    const t = parseTransaction({ TradePrice: '30000000', Period: '2024年第3四半期', Type: '宅地(土地)' })!;
    expect(t.areaM2).toBeNull();
    expect(t.coverageRatioPct).toBeNull();
    expect(t.stationMinutes).toBeNull();
  });
  it('rejects zero / missing price', () => {
    expect(parseTransaction({ TradePrice: '', Period: '2024年第3四半期' })).toBeNull();
    expect(parseTransaction({ TradePrice: '0', Period: '2024年第3四半期' })).toBeNull();
  });
});

describe('XPT002 / XKT002 parsers', () => {
  it('land price points', () => {
    const pts = parseLandPrices(xpt002 as GeoJsonFC);
    expect(pts).toHaveLength(2);
    expect(pts[0]).toMatchObject({ kind: '地価公示', pricePerM2: 512000, yoyChangePct: 2.3, year: 2025, coverageRatioPct: 60, floorAreaRatioPct: 150, stationDistanceM: 850 });
    expect(pts[1]!.kind).toBe('都道府県地価調査');
  });
  it('zoning at point', () => {
    const z = parseZoningAt(xkt002 as GeoJsonFC, P)!;
    expect(z).toMatchObject({ useDistrict: '第一種低層住居専用地域', coverageRatioPct: 60, floorAreaRatioPct: 150 });
    expect(parseZoningAt(xkt002 as GeoJsonFC, { lat: 0, lng: 0 })).toBeNull();
  });
});

describe('RealEstateDataService', () => {
  const svc = (c = new MockReinfolibClient()) => new RealEstateDataService(c, NOW);

  it('mock data flows through the real parsers and is labelled mock', async () => {
    const z = await svc().zoning(P);
    expect(z.status).toBe('available');
    expect(z.sources[0]!.mode).toBe('mock');
    expect(z.sources[0]!.name).toContain('モック');
    const lp = await svc().landPrices(P);
    expect(lp.status).toBe('available');
    if (lp.status === 'available') {
      expect(lp.value.length).toBeGreaterThan(0);
      expect(lp.value[0]!.distanceM).toBeLessThanOrEqual(lp.value.at(-1)!.distanceM!);
    }
    const tx = await svc().transactions('13112');
    expect(tx.status).toBe('available');
  });

  it('API failure -> unavailable (確認できず), not empty', async () => {
    const s = svc(new MockReinfolibClient({ failApis: ['XKT002', 'XPT002', 'XIT001'] }));
    expect((await s.zoning(P)).status).toBe('unavailable');
    expect((await s.landPrices(P)).status).toBe('unavailable');
    expect((await s.transactions('13112')).status).toBe('unavailable');
  });

  it('empty response -> no_data (データなし)', async () => {
    const s = svc(new MockReinfolibClient({ emptyApis: ['XKT002', 'XPT002', 'XIT001'] }));
    const z = await s.zoning(P);
    expect(z.status).toBe('no_data');
    expect(z.value).toBeNull();
    expect((await s.landPrices(P)).status).toBe('no_data');
    expect((await s.transactions('13112')).status).toBe('no_data');
  });

  it('no city code -> unavailable', async () => {
    expect((await svc().transactions(null)).status).toBe('unavailable');
  });

  it('falls back to previous year for land prices', async () => {
    const years: number[] = [];
    const client = new MockReinfolibClient();
    const orig = client.tile.bind(client);
    client.tile = async (api, t, extra) => {
      years.push(Number(extra?.year));
      return Number(extra?.year) === 2026 ? { type: 'FeatureCollection', features: [] } : orig(api, t, extra);
    };
    const r = await svc(client).landPrices(P);
    expect(r.status).toBe('available');
    expect(years).toContain(2025);
  });
});

describe('GSI reverse geocoder', () => {
  it('pads muniCd and keeps town level only', async () => {
    const g = new GsiReverseGeocoder({ fetch: async () => new Response(JSON.stringify({ results: { muniCd: '1101', lv01Nm: '北一条西二丁目' } })) });
    expect(await g.reverse(P)).toMatchObject({ cityCode: '01101', prefectureCode: '01', townName: '北一条西二丁目' });
  });
  it('no result -> null', async () => {
    const g = new GsiReverseGeocoder({ fetch: async () => new Response(JSON.stringify({})) });
    expect(await g.reverse(P)).toBeNull();
  });
});

describe('tileBbox', () => {
  it('contains the point of its tile', () => {
    const t = lngLatToTile(P, 15);
    const b = tileBbox(t.x, t.y, t.z);
    expect(P.lng).toBeGreaterThan(b.w);
    expect(P.lng).toBeLessThan(b.e);
    expect(P.lat).toBeGreaterThan(b.s);
    expect(P.lat).toBeLessThan(b.n);
  });
});
