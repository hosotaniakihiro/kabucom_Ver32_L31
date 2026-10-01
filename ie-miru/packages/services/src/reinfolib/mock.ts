import type { GeoJsonFC, ReinfolibClient, TileApi, Xit001Params } from './client';

/**
 * APIキーが無い環境用のモック。**実データではない**。
 * 返す JSON の形は本番 API と同じにして、同じパーサ・同じ画面を通す（契約テストを兼ねる）。
 * 値はタイル座標・市区町村コードから決定的に生成する。UI では SourceRef.mode='mock' により「モックデータ」と表示される。
 */
export class MockReinfolibClient implements ReinfolibClient {
  readonly mode = 'mock' as const;
  /** テストで API 障害を再現するためのフック */
  constructor(private readonly opts: { failApis?: Array<TileApi | 'XIT001'>; emptyApis?: Array<TileApi | 'XIT001'> } = {}) {}

  private check(api: TileApi | 'XIT001') {
    if (this.opts.failApis?.includes(api)) throw Object.assign(new Error(`mock failure ${api}`), { kind: 'http' });
    return !this.opts.emptyApis?.includes(api);
  }

  async transactions(p: Xit001Params): Promise<Record<string, unknown>[]> {
    if (!this.check('XIT001')) return [];
    const seed = Number(p.city ?? p.area ?? '13000') + p.year * 7;
    const rows: Record<string, unknown>[] = [];
    const quarters = p.quarter ? [p.quarter] : [1, 2, 3, 4];
    for (const q of quarters) {
      for (let i = 0; i < 6; i++) {
        const r = rand(seed, q * 10 + i);
        const isHouse = i % 3 !== 2;
        const land = Math.round(70 + r * 110);
        const unit = Math.round((250_000 + rand(seed, i + 99) * 350_000) / 1000) * 1000;
        const age = Math.floor(rand(seed, i + 7) * 40);
        const floor = Math.round(land * (0.9 + rand(seed, i + 3) * 0.6));
        const bldgValue = isHouse ? Math.max(0.1, 1 - age / 30) * floor * 200_000 : 0;
        const price = Math.round((land * unit + bldgValue) / 1_000_000) * 1_000_000;
        rows.push({
          PriceCategory: q % 2 === 0 ? '成約価格情報' : '不動産取引価格情報',
          Type: isHouse ? '宅地(土地と建物)' : '宅地(土地)',
          Region: '住宅地',
          MunicipalityCode: p.city ?? '13112',
          Prefecture: 'モック県',
          Municipality: 'モック市',
          DistrictName: ['モック一丁目', 'モック二丁目', 'サンプル町'][i % 3],
          TradePrice: String(price),
          PricePerUnit: '',
          FloorPlan: isHouse ? ['3LDK', '4LDK', '2LDK'][i % 3] : '',
          Area: String(land),
          UnitPrice: isHouse ? '' : String(unit),
          LandShape: 'ほぼ長方形',
          Frontage: '8.0',
          TotalFloorArea: isHouse ? String(floor) : '',
          BuildingYear: isHouse ? `${2024 - age}年` : '',
          Structure: isHouse ? (i % 2 ? '木造' : '軽量鉄骨造') : '',
          Use: isHouse ? '住宅' : '',
          Purpose: '住宅',
          Direction: '南',
          Classification: '区道',
          Breadth: '4.0',
          CityPlanning: '第一種低層住居専用地域',
          CoverageRatio: '60',
          FloorAreaRatio: '150',
          Period: `${p.year}年第${q}四半期`,
          Renovation: '',
          Remarks: '',
        });
      }
    }
    return rows;
  }

  async tile(api: TileApi, t: { z: number; x: number; y: number }, extra: Record<string, string | number> = {}): Promise<GeoJsonFC> {
    if (!this.check(api)) return { type: 'FeatureCollection', features: [] };
    const bbox = tileBbox(t.x, t.y, t.z);
    const r = rand(t.x * 31 + t.y, t.z);
    const whole = [[[bbox.w, bbox.s], [bbox.e, bbox.s], [bbox.e, bbox.n], [bbox.w, bbox.n], [bbox.w, bbox.s]]];
    const westHalf = [[[bbox.w, bbox.s], [(bbox.w + bbox.e) / 2, bbox.s], [(bbox.w + bbox.e) / 2, bbox.n], [bbox.w, bbox.n], [bbox.w, bbox.s]]];
    const poly = (props: Record<string, unknown>, coordinates = whole) => ({ type: 'FeatureCollection' as const, features: [{ type: 'Feature' as const, properties: props, geometry: { type: 'Polygon', coordinates } }] });
    switch (api) {
      case 'XKT002': {
        const zones = [
          ['第一種低層住居専用地域', '50%', '100%'],
          ['第一種低層住居専用地域', '60%', '150%'],
          ['第一種中高層住居専用地域', '60%', '200%'],
          ['第一種住居地域', '60%', '200%'],
          ['近隣商業地域', '80%', '300%'],
        ][Math.floor(r * 5)]!;
        return poly({ use_area_ja: zones[0], u_building_coverage_ratio_ja: zones[1], u_floor_area_ratio_ja: zones[2], prefecture: 'モック県', city_name: 'モック市' });
      }
      case 'XKT014':
        return r > 0.5 ? poly({ fire_prevention_ja: '準防火地域' }) : { type: 'FeatureCollection', features: [] };
      case 'XPT002': {
        const year = Number(extra.year ?? 2025);
        const feats = [0, 1, 2, 3].map((i) => {
          const lr = rand(t.x + i, t.y - i);
          const lng = bbox.w + (bbox.e - bbox.w) * (0.15 + 0.7 * rand(i, t.x));
          const lat = bbox.s + (bbox.n - bbox.s) * (0.15 + 0.7 * rand(i, t.y));
          const price = Math.round((280_000 + lr * 320_000) / 1000) * 1000;
          return {
            type: 'Feature' as const,
            properties: {
              point_id: `MOCK-${t.x}-${t.y}-${i}`,
              target_year_name_ja: `令和${year - 2018}年`,
              land_price_type: i % 2 === 0 ? 0 : 1,
              u_current_years_price_ja: `${price.toLocaleString('en-US')}(円/㎡)`,
              year_on_year_change_rate: (Math.round((lr * 6 - 1) * 10) / 10).toString(),
              residence_display_name_ja: `モック市モック${i + 1}丁目`,
              use_category_name_ja: '住宅',
              regulations_use_category_name_ja: '第一種低層住居専用地域',
              u_regulations_building_coverage_ratio_ja: '60(%)',
              u_regulations_floor_area_ratio_ja: '150(%)',
              nearest_station_name_ja: 'モック駅',
              u_road_distance_to_nearest_station_name_ja: `${Math.round(400 + lr * 1200)}m`,
            },
            geometry: { type: 'Point', coordinates: [lng, lat] },
          };
        });
        return { type: 'FeatureCollection', features: feats };
      }
      case 'XKT026':
        return r > 0.4 ? poly({ A31a_205: '0.5m以上3.0m未満', river_name: 'モック川' }, westHalf) : { type: 'FeatureCollection', features: [] };
      case 'XKT027':
        return r > 0.8 ? poly({ A49_003: '0.3m以上0.5m未満' }, westHalf) : { type: 'FeatureCollection', features: [] };
      case 'XKT028':
        return r > 0.85 ? poly({ A40_003: '1.0m以上2.0m未満' }, westHalf) : { type: 'FeatureCollection', features: [] };
      case 'XKT029':
        return r > 0.75 ? poly({ A33_001: '1', A33_002: '1' }, westHalf) : { type: 'FeatureCollection', features: [] };
      case 'XKT025':
        return poly({ liquefaction_tendency_level: r > 0.5 ? '液状化しやすい' : '液状化しにくい' });
      case 'XKT016':
        return { type: 'FeatureCollection', features: [] };
    }
  }
}

function rand(a: number, b: number): number {
  let h = Math.imul(a | 0, 2654435761) ^ Math.imul((b | 0) + 0x9e3779b9, 1597334677);
  h = Math.imul(h ^ (h >>> 15), 2246822519);
  h ^= h >>> 13;
  return (h >>> 0) / 4294967295;
}

export function tileBbox(x: number, y: number, z: number) {
  const n = 2 ** z;
  const lng = (xx: number) => (xx / n) * 360 - 180;
  const lat = (yy: number) => (Math.atan(Math.sinh(Math.PI * (1 - (2 * yy) / n))) * 180) / Math.PI;
  return { w: lng(x), e: lng(x + 1), n: lat(y), s: lat(y + 1) };
}
