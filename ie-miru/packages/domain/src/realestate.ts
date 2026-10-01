import type { LatLng } from './geo';

/** 取引の種類（不動産情報ライブラリ XIT001 の Type） */
export type TransactionType = '宅地(土地と建物)' | '宅地(土地)' | '中古マンション等' | '農地' | '林地' | 'その他';

/**
 * 周辺取引事例（1件）。この建物そのものの価格ではない。
 * 欠損値は null。
 */
export interface Transaction {
  id: string;
  /** 取引価格情報 / 成約価格情報 */
  priceCategory: 'transaction' | 'contract' | null;
  type: TransactionType;
  prefecture: string | null;
  municipality: string | null;
  municipalityCode: string | null;
  district: string | null;
  /** 総額 [円] */
  priceYen: number;
  /** 面積 [m²]（土地面積。マンションは専有面積） */
  areaM2: number | null;
  /** 面積が「2000㎡以上」等の上限表記だった */
  areaIsLowerBound: boolean;
  /** 土地の㎡単価 [円/m²]（土地取引のみ公表） */
  unitPriceYenPerM2: number | null;
  totalFloorAreaM2: number | null;
  buildingYear: number | null;
  structure: string | null;
  use: string | null;
  floorPlan: string | null;
  /** 都市計画（用途地域） */
  cityPlanning: string | null;
  coverageRatioPct: number | null;
  floorAreaRatioPct: number | null;
  frontRoadWidthM: number | null;
  /** 最寄駅までの時間 [分]（提供される場合のみ） */
  stationMinutes: number | null;
  /** 取引時期 */
  year: number;
  quarter: 1 | 2 | 3 | 4 | null;
  remarks: string | null;
}

/** 地価公示・都道府県地価調査の地点 */
export interface LandPricePoint {
  id: string;
  kind: '地価公示' | '都道府県地価調査';
  year: number;
  /** [円/m²] */
  pricePerM2: number;
  /** 前年比 [%]。不明なら null */
  yoyChangePct: number | null;
  /** 所在（標準地の住居表示。公示情報） */
  address: string | null;
  useCategory: string | null;
  zoning: string | null;
  coverageRatioPct: number | null;
  floorAreaRatioPct: number | null;
  nearestStation: string | null;
  stationDistanceM: number | null;
  location: LatLng;
  /** 対象地点からの距離 [m]（集計時に付与） */
  distanceM?: number;
}

/** 用途地域・都市計画 */
export interface ZoningInfo {
  useDistrict: string;
  coverageRatioPct: number | null;
  floorAreaRatioPct: number | null;
  /** 防火地域・準防火地域（取れた場合） */
  firePrevention: string | null;
  /** 高度地区等の補足 */
  notes: string[];
}

/** 「1990年」「平成2年」「昭和45年」「令和3年」「戦前」→ 西暦。不明は null */
export function parseJapaneseYear(s: string | null | undefined): number | null {
  if (!s) return null;
  const t = String(s).trim();
  const western = /(\d{4})\s*年?/.exec(t);
  if (western) return Number(western[1]);
  const eras: Array<[RegExp, number]> = [
    [/令和\s*(元|\d+)/, 2018],
    [/平成\s*(元|\d+)/, 1988],
    [/昭和\s*(元|\d+)/, 1925],
    [/大正\s*(元|\d+)/, 1911],
  ];
  for (const [re, base] of eras) {
    const m = re.exec(t);
    if (m) return base + (m[1] === '元' ? 1 : Number(m[1]));
  }
  if (/戦前/.test(t)) return 1945; // 下限として扱う
  return null;
}

/** "123,000(円/㎡)" "60%" "2000㎡以上" → 数値。取れなければ null（0 にしない） */
export function parseLooseNumber(s: unknown): number | null {
  if (s == null) return null;
  if (typeof s === 'number') return Number.isFinite(s) ? s : null;
  const m = /-?[\d,]+(?:\.\d+)?/.exec(String(s));
  if (!m) return null;
  const n = Number(m[0].replace(/,/g, ''));
  return Number.isFinite(n) ? n : null;
}

/** "2023年第1四半期" → {year, quarter} */
export function parsePeriod(s: string | null | undefined): { year: number; quarter: 1 | 2 | 3 | 4 | null } | null {
  if (!s) return null;
  const y = parseJapaneseYear(s);
  if (y == null) return null;
  const q = /第\s*([1-4１-４])\s*四半期/.exec(s);
  const qn = q ? Number(q[1]!.replace(/[１-４]/, (c) => String('１２３４'.indexOf(c) + 1))) : null;
  return { year: y, quarter: (qn as 1 | 2 | 3 | 4 | null) ?? null };
}
