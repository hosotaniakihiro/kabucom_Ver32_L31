import { estimateFloorsFromHeight, type Building } from './building';
import type { Transaction, TransactionType, ZoningInfo } from './realestate';
import { normalizeUseDistrict } from './zoning';

/**
 * 対象物件の推定プロファイル。建物データ・用途地域から機械的に推定した値（AI推定）であり、
 * 利用者が「買う/売る」画面で上書きできる。
 */
export interface PropertySubject {
  transactionType: TransactionType;
  /** 土地面積 [m²]（推定）。不明なら null */
  landAreaM2: number | null;
  landAreaEstimated: boolean;
  /** 延床面積 [m²]（推定） */
  floorAreaM2: number | null;
  floorAreaEstimated: boolean;
  builtYear: number | null;
  structure: string | null;
  useDistrict: string | null;
  stationMinutes: number | null;
  townName: string | null;
}

export function estimateSubject(b: Building, zoning: ZoningInfo | null, opts: { townName?: string | null; overrides?: Partial<PropertySubject> } = {}): PropertySubject {
  const floors = b.floorsAbove ?? estimateFloorsFromHeight(b.heightM);
  const coverage = zoning?.coverageRatioPct ?? null;
  // 建物の投影面積 ÷（建ぺい率 × 消化率0.85）で敷地面積を推定。不明なら建ぺい率50%を仮定
  const ratio = (coverage ?? 50) / 100 * 0.85;
  const land = b.footprintAreaM2 > 0 ? Math.min(b.footprintAreaM2 * 4, Math.max(b.footprintAreaM2, b.footprintAreaM2 / ratio)) : null;
  const floor = b.footprintAreaM2 > 0 && floors != null ? b.footprintAreaM2 * floors * 0.95 : null;
  // 戸建・共同住宅とも一棟（土地と建物）として比較する
  const type: TransactionType = '宅地(土地と建物)';
  const base: PropertySubject = {
    transactionType: type,
    landAreaM2: land == null ? null : Math.round(land),
    landAreaEstimated: true,
    floorAreaM2: floor == null ? null : Math.round(floor),
    floorAreaEstimated: true,
    builtYear: b.builtYear,
    structure: b.structure,
    useDistrict: zoning?.useDistrict ?? null,
    stationMinutes: null,
    townName: opts.townName ?? null,
  };
  const o = opts.overrides ?? {};
  return {
    ...base,
    ...o,
    landAreaEstimated: o.landAreaM2 != null ? false : base.landAreaEstimated,
    floorAreaEstimated: o.floorAreaM2 != null ? false : base.floorAreaEstimated,
  };
}

export interface SimilarityBreakdown {
  score: number;
  /** 比較に使えた観点 */
  used: string[];
  /** 欠損で比較できなかった観点（欠損は 0 点扱いにしない） */
  skipped: string[];
}

const W = { type: 0.3, area: 0.2, age: 0.2, station: 0.1, zoning: 0.1, district: 0.1 };

export function similarity(s: PropertySubject, t: Transaction): SimilarityBreakdown {
  const parts: Array<[keyof typeof W, number | null]> = [];
  // 種別
  const typeScore = t.type === s.transactionType ? 1 : t.type === '宅地(土地)' && s.transactionType === '宅地(土地と建物)' ? 0.35 : t.type === '中古マンション等' ? 0.1 : 0;
  parts.push(['type', typeScore]);
  // 面積（対数比）
  parts.push(['area', s.landAreaM2 != null && t.areaM2 != null && t.areaM2 > 0 && !t.areaIsLowerBound ? Math.exp(-Math.abs(Math.log(s.landAreaM2 / t.areaM2)) * 1.5) : null]);
  // 築年（土地のみの取引は比較不能）
  parts.push(['age', s.builtYear != null && t.buildingYear != null ? 1 - Math.min(1, Math.abs(s.builtYear - t.buildingYear) / 25) : null]);
  // 駅距離
  parts.push(['station', s.stationMinutes != null && t.stationMinutes != null ? 1 - Math.min(1, Math.abs(s.stationMinutes - t.stationMinutes) / 15) : null]);
  // 用途地域
  const sz = normalizeUseDistrict(s.useDistrict);
  const tz = normalizeUseDistrict(t.cityPlanning);
  parts.push(['zoning', sz && tz ? (sz === tz ? 1 : sz.slice(0, 3) === tz.slice(0, 3) ? 0.5 : 0.2) : null]);
  // 町丁
  parts.push(['district', s.townName && t.district ? (stripChome(s.townName) === stripChome(t.district) ? 1 : 0.4) : null]);

  let wSum = 0;
  let acc = 0;
  const used: string[] = [];
  const skipped: string[] = [];
  for (const [k, v] of parts) {
    if (v == null) {
      skipped.push(k);
      continue;
    }
    wSum += W[k];
    acc += W[k] * v;
    used.push(k);
  }
  // 比較できた観点が少ないほど信頼度を下げる（重みの合計で減衰）
  const coverage = wSum;
  const score = wSum > 0 ? (acc / wSum) * (0.6 + 0.4 * coverage) : 0;
  return { score: Math.round(score * 1000) / 1000, used, skipped };
}

function stripChome(s: string) {
  return s.replace(/[一二三四五六七八九十\d０-９]+丁目.*$/, '').replace(/\s/g, '');
}

export interface ComparableItem {
  transaction: Transaction;
  similarity: number;
  used: string[];
}

export interface ComparablesSummary {
  /** 類似事例として採用した件数 */
  count: number;
  /** 母数（期間内の取引件数） */
  totalInArea: number;
  items: ComparableItem[];
  medianPriceYen: number | null;
  p25PriceYen: number | null;
  p75PriceYen: number | null;
  /** 土地面積あたりの総額単価（土地と建物の取引）または土地㎡単価 [円/m²] */
  medianUnitPriceYenPerM2: number | null;
  /** 土地のみ取引の㎡単価中央値 */
  medianLandUnitPriceYenPerM2: number | null;
  notes: string[];
}

export function quantile(sorted: number[], q: number): number | null {
  if (sorted.length === 0) return null;
  const pos = (sorted.length - 1) * q;
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  return sorted[lo]! + (sorted[hi]! - sorted[lo]!) * (pos - lo);
}

export function summarizeComparables(subject: PropertySubject, txs: Transaction[], opts: { minSimilarity?: number; limit?: number; sinceYear?: number } = {}): ComparablesSummary {
  const since = opts.sinceYear ?? -Infinity;
  const pool = txs.filter((t) => t.year >= since && t.type !== '農地' && t.type !== '林地');
  const scored = pool
    .map((t) => ({ transaction: t, ...similarity(subject, t) }))
    .map(({ transaction, score, used }) => ({ transaction, similarity: score, used }))
    .filter((x) => x.similarity >= (opts.minSimilarity ?? 0.45))
    .sort((a, b) => b.similarity - a.similarity || b.transaction.year - a.transaction.year);
  const items = scored.slice(0, opts.limit ?? 10);
  const prices = items.map((i) => i.transaction.priceYen).sort((a, b) => a - b);
  const units = items
    .filter((i) => i.transaction.areaM2 && !i.transaction.areaIsLowerBound)
    .map((i) => i.transaction.unitPriceYenPerM2 ?? i.transaction.priceYen / i.transaction.areaM2!)
    .sort((a, b) => a - b);
  const landUnits = pool
    .filter((t) => t.type === '宅地(土地)')
    .map((t) => t.unitPriceYenPerM2 ?? (t.areaM2 && !t.areaIsLowerBound ? t.priceYen / t.areaM2 : null))
    .filter((v): v is number => v != null)
    .sort((a, b) => a - b);
  const notes: string[] = ['周辺の取引事例であり、この建物そのものの売買価格ではありません。'];
  if (items.length < 3) notes.push('類似する取引事例が少ないため、参考相場の信頼度は低くなります。');
  return {
    count: items.length,
    totalInArea: pool.length,
    items,
    medianPriceYen: quantile(prices, 0.5),
    p25PriceYen: quantile(prices, 0.25),
    p75PriceYen: quantile(prices, 0.75),
    medianUnitPriceYenPerM2: quantile(units, 0.5),
    medianLandUnitPriceYenPerM2: quantile(landUnits, 0.5),
    notes,
  };
}
