import {
  normalizeUseDistrict, parseJapaneseYear, parseLooseNumber, parsePeriod, pointInPolygon,
  type LandPricePoint, type LatLng, type Ring, type Transaction, type TransactionType, type ZoningInfo,
} from '@ie-miru/domain';
import type { GeoJsonFC, GeoJsonFeature } from './client';

const norm = (k: string) => k.toLowerCase().replace(/[^a-z0-9぀-ヿ一-鿿]/g, '');

export function pickProp(props: Record<string, unknown> | null | undefined, keys: string[]): unknown {
  if (!props) return undefined;
  const m = new Map<string, unknown>();
  for (const [k, v] of Object.entries(props)) m.set(norm(k), v);
  for (const k of keys) {
    const v = m.get(norm(k));
    if (v !== undefined && v !== null && v !== '') return v;
  }
  return undefined;
}

const str = (v: unknown) => (v == null || v === '' ? null : String(v));

const TYPES: TransactionType[] = ['宅地(土地と建物)', '宅地(土地)', '中古マンション等', '農地', '林地'];

/** XIT001 の1行 → Transaction。価格が無い行は捨てる（0円として扱わない） */
export function parseTransaction(row: Record<string, unknown>, index = 0): Transaction | null {
  const price = parseLooseNumber(row.TradePrice);
  if (price == null || price <= 0) return null;
  const period = parsePeriod(str(row.Period));
  if (!period) return null;
  const areaRaw = str(row.Area);
  const typeRaw = str(row.Type) ?? '';
  const type: TransactionType = TYPES.find((t) => t === typeRaw) ?? 'その他';
  const cat = str(row.PriceCategory);
  return {
    id: `xit001:${row.MunicipalityCode ?? ''}:${row.Period ?? ''}:${index}`,
    priceCategory: cat?.includes('成約') ? 'contract' : cat ? 'transaction' : null,
    type,
    prefecture: str(row.Prefecture),
    municipality: str(row.Municipality),
    municipalityCode: str(row.MunicipalityCode),
    district: str(row.DistrictName),
    priceYen: price,
    areaM2: parseLooseNumber(areaRaw),
    areaIsLowerBound: !!areaRaw && /以上/.test(areaRaw),
    unitPriceYenPerM2: parseLooseNumber(row.UnitPrice),
    totalFloorAreaM2: parseLooseNumber(row.TotalFloorArea),
    buildingYear: parseJapaneseYear(str(row.BuildingYear)),
    structure: str(row.Structure),
    use: str(row.Use),
    floorPlan: str(row.FloorPlan),
    cityPlanning: str(row.CityPlanning),
    coverageRatioPct: parseLooseNumber(row.CoverageRatio),
    floorAreaRatioPct: parseLooseNumber(row.FloorAreaRatio),
    frontRoadWidthM: parseLooseNumber(row.Breadth),
    stationMinutes: parseLooseNumber(row.TimeToNearestStation),
    year: period.year,
    quarter: period.quarter,
    remarks: str(row.Remarks),
  };
}

export function parseTransactions(rows: Record<string, unknown>[]): Transaction[] {
  return rows.map((r, i) => parseTransaction(r, i)).filter((t): t is Transaction => t !== null);
}

/** XPT002 GeoJSON → 地価ポイント */
export function parseLandPrices(fc: GeoJsonFC): LandPricePoint[] {
  const out: LandPricePoint[] = [];
  for (const f of fc.features) {
    if (f.geometry?.type !== 'Point') continue;
    const [lng, lat] = f.geometry.coordinates as number[];
    const p = f.properties ?? {};
    const price = parseLooseNumber(pickProp(p, ['u_current_years_price_ja', 'current_years_price', 'price']));
    if (price == null || price <= 0 || lat == null || lng == null) continue;
    const kindRaw = pickProp(p, ['land_price_type', 'price_classification', 'land_price_type_name_ja']);
    const kind = kindRaw === 1 || kindRaw === '1' || /調査/.test(String(kindRaw ?? '')) ? '都道府県地価調査' : '地価公示';
    const yoy = parseLooseNumber(pickProp(p, ['year_on_year_change_rate', 'u_year_on_year_change_rate_ja']));
    out.push({
      id: String(pickProp(p, ['point_id', 'standard_lot_number_ja', 'id']) ?? `${lat},${lng}`),
      kind,
      year: parseJapaneseYear(str(pickProp(p, ['target_year_name_ja', 'target_year']))) ?? new Date().getFullYear(),
      pricePerM2: price,
      yoyChangePct: yoy,
      address: str(pickProp(p, ['residence_display_name_ja', 'place_name_ja', 'location_number_ja'])),
      useCategory: str(pickProp(p, ['use_category_name_ja', 'usage_status_name_ja'])),
      zoning: str(pickProp(p, ['regulations_use_category_name_ja', 'usage_area_name_ja'])),
      coverageRatioPct: parseLooseNumber(pickProp(p, ['u_regulations_building_coverage_ratio_ja', 'building_coverage_ratio'])),
      floorAreaRatioPct: parseLooseNumber(pickProp(p, ['u_regulations_floor_area_ratio_ja', 'floor_area_ratio'])),
      nearestStation: str(pickProp(p, ['nearest_station_name_ja'])),
      stationDistanceM: parseLooseNumber(pickProp(p, ['u_road_distance_to_nearest_station_name_ja', 'road_distance_to_nearest_station'])),
      location: { lat, lng },
    });
  }
  return out;
}

export function featureRings(f: GeoJsonFeature): Ring[] {
  const g = f.geometry;
  if (!g) return [];
  const toRing = (c: number[][]) => c.map(([lng, lat]) => ({ lat: lat!, lng: lng! }));
  if (g.type === 'Polygon') return [(g.coordinates as number[][][])[0]!].filter(Boolean).map(toRing);
  if (g.type === 'MultiPolygon') return (g.coordinates as number[][][][]).map((p) => toRing(p[0]!));
  return [];
}

/** ポリゴン（穴は無視）に点が含まれる feature を返す */
export function featuresContaining(fc: GeoJsonFC, p: LatLng): GeoJsonFeature[] {
  return fc.features.filter((f) => featureRings(f).some((r) => pointInPolygon(p, r)));
}

/** XKT002 用途地域 → ZoningInfo（地点を含むポリゴン）。該当なしは null */
export function parseZoningAt(fc: GeoJsonFC, p: LatLng, fire?: GeoJsonFC): ZoningInfo | null {
  const hit = featuresContaining(fc, p)[0];
  if (!hit) return null;
  const props = hit.properties ?? {};
  const raw = str(pickProp(props, ['use_area_ja', 'youto_name', 'use_district', 'youto_id_name']));
  const d = normalizeUseDistrict(raw);
  const fireHit = fire ? featuresContaining(fire, p)[0] : undefined;
  return {
    useDistrict: d ?? raw ?? '不明',
    coverageRatioPct: parseLooseNumber(pickProp(props, ['u_building_coverage_ratio_ja', 'building_coverage_ratio'])),
    floorAreaRatioPct: parseLooseNumber(pickProp(props, ['u_floor_area_ratio_ja', 'floor_area_ratio'])),
    firePrevention: fireHit ? str(pickProp(fireHit.properties, ['fire_prevention_ja', 'boka_name', 'name'])) : null,
    notes: [],
  };
}
