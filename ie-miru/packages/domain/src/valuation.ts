import { structureClassOf, type StructureClass } from './building';
import type { ComparablesSummary, PropertySubject } from './comparables';
import type { LandPricePoint } from './realestate';

/**
 * AI参考査定（説明可能なルールベース推定）。正式な不動産鑑定・査定ではない。
 * 必ずレンジで返し、一点価格だけを返さない。
 */
export interface Valuation {
  estimatedLow: number;
  estimatedMid: number;
  estimatedHigh: number;
  /** 0..1 */
  confidence: number;
  confidenceLabel: '高' | '中' | '低';
  reasons: string[];
  components: {
    landUnitPriceYenPerM2: number | null;
    landValueYen: number | null;
    buildingValueYen: number | null;
    costApproachYen: number | null;
    comparableApproachYen: number | null;
  };
  method: 'cost+comparables' | 'cost' | 'comparables';
}

export type ValuationResult = { ok: true; valuation: Valuation } | { ok: false; reason: string };

/** 再調達原価 [円/m²]（概算・参考値） */
export const REPLACEMENT_COST_PER_M2: Record<StructureClass, number> = {
  wood: 200_000, light_steel: 230_000, steel: 260_000, rc: 300_000, src: 330_000, masonry: 220_000, unknown: 220_000,
};
/** 法定耐用年数（住宅用） */
export const USEFUL_LIFE_YEARS: Record<StructureClass, number> = {
  wood: 22, light_steel: 27, steel: 34, rc: 47, src: 47, masonry: 38, unknown: 30,
};
const STRUCTURE_JA: Record<StructureClass, string> = {
  wood: '木造', light_steel: '軽量鉄骨造', steel: '鉄骨造', rc: '鉄筋コンクリート造', src: '鉄骨鉄筋コンクリート造', masonry: 'ブロック造等', unknown: '構造不明',
};
/** 公示価格は実勢価格の概ね9割とされることからの補正係数 */
export const PUBLIC_PRICE_TO_MARKET = 1.1;

const man = (yen: number) => `${Math.round(yen / 10_000).toLocaleString('ja-JP')}万円`;
const unitMan = (yen: number) => `${(yen / 10_000).toFixed(1)}万円/㎡`;

/** 距離の逆数で重み付けした地価（最大3地点・1.5km以内） */
export function weightedLandPrice(points: LandPricePoint[]): { price: number; used: LandPricePoint[] } | null {
  const near = points.filter((p) => (p.distanceM ?? Infinity) <= 1500).slice(0, 3);
  if (near.length === 0) return null;
  let w = 0;
  let s = 0;
  for (const p of near) {
    const wi = 1 / Math.max(100, p.distanceM ?? 1500);
    w += wi;
    s += wi * p.pricePerM2;
  }
  return { price: s / w, used: near };
}

export function buildingResidualRatio(structure: StructureClass, age: number | null): number {
  if (age == null) return 0.4;
  return Math.max(0.1, 1 - age / USEFUL_LIFE_YEARS[structure]);
}

export function estimateValue(input: {
  subject: PropertySubject;
  landPrices: LandPricePoint[] | null;
  comparables: ComparablesSummary | null;
  now?: Date;
}): ValuationResult {
  const { subject, comparables } = input;
  const year = (input.now ?? new Date()).getFullYear();
  const reasons: string[] = [];
  let spread = 0.08;

  if (subject.landAreaM2 == null || subject.landAreaM2 <= 0) return { ok: false, reason: '土地面積を推定できないため査定できません' };
  reasons.push(`土地面積 ${Math.round(subject.landAreaM2)}㎡${subject.landAreaEstimated ? '（建物外形と建ぺい率からの推定）' : '（入力値）'}`);
  if (subject.landAreaEstimated) spread += 0.06;

  // ── 積算（原価）アプローチ ──
  let landUnit: number | null = null;
  const lp = input.landPrices ? weightedLandPrice(input.landPrices) : null;
  if (lp) {
    landUnit = lp.price * PUBLIC_PRICE_TO_MARKET;
    const nearest = lp.used[0]!;
    reasons.push(`近隣の${nearest.kind} ${unitMan(nearest.pricePerM2)}（約${Math.round(nearest.distanceM ?? 0)}m）ほか${lp.used.length}地点を距離で加重し、実勢補正×${PUBLIC_PRICE_TO_MARKET}`);
  } else if (comparables?.medianLandUnitPriceYenPerM2 != null) {
    landUnit = comparables.medianLandUnitPriceYenPerM2;
    reasons.push(`地価公示地点が近くにないため、周辺の土地取引の㎡単価 中央値 ${unitMan(landUnit)} を使用`);
    spread += 0.04;
  }

  const sc = structureClassOf(subject.structure);
  const age = subject.builtYear != null ? Math.max(0, year - subject.builtYear) : null;
  let buildingValue: number | null = null;
  if (subject.floorAreaM2 != null && subject.floorAreaM2 > 0) {
    const ratio = buildingResidualRatio(sc, age);
    buildingValue = subject.floorAreaM2 * REPLACEMENT_COST_PER_M2[sc] * ratio;
    reasons.push(
      `建物: ${STRUCTURE_JA[sc]}・延床${Math.round(subject.floorAreaM2)}㎡${subject.floorAreaEstimated ? '（推定）' : ''}・${age != null ? `築${age}年` : '築年不明'} → 再調達原価${unitMan(REPLACEMENT_COST_PER_M2[sc])}×残価率${Math.round(ratio * 100)}%`,
    );
    if (age == null) {
      spread += 0.05;
      reasons.push('築年が不明なため推定幅を広げています');
    }
    if (sc === 'unknown') spread += 0.03;
    if (subject.floorAreaEstimated) spread += 0.02;
  } else {
    reasons.push('延床面積が不明なため建物価格は 0 ではなく「不明」として土地のみで評価');
    spread += 0.06;
  }

  const landValue = landUnit != null ? landUnit * subject.landAreaM2 : null;
  const costApproach = landValue != null ? landValue + (buildingValue ?? 0) : null;

  // ── 取引事例アプローチ ──
  let compApproach: number | null = null;
  if (comparables && comparables.count > 0 && comparables.medianUnitPriceYenPerM2 != null) {
    compApproach = comparables.medianUnitPriceYenPerM2 * subject.landAreaM2;
    reasons.push(`類似する周辺取引事例 ${comparables.count}件の土地面積あたり総額 中央値 ${unitMan(comparables.medianUnitPriceYenPerM2)} × 土地面積`);
    if (comparables.count < 3) spread += 0.05;
    if (comparables.p25PriceYen != null && comparables.p75PriceYen != null && comparables.medianPriceYen) {
      const disp = (comparables.p75PriceYen - comparables.p25PriceYen) / comparables.medianPriceYen / 2;
      spread += Math.min(0.1, disp * 0.5);
    }
  } else {
    reasons.push('類似する取引事例が無いため、事例による補正をしていません');
    spread += 0.05;
  }

  if (costApproach == null && compApproach == null) return { ok: false, reason: '地価・取引事例のどちらも得られないため査定できません' };

  let mid: number;
  let method: Valuation['method'];
  if (costApproach != null && compApproach != null) {
    const wComp = (comparables?.count ?? 0) >= 5 ? 0.6 : 0.4;
    mid = costApproach * (1 - wComp) + compApproach * wComp;
    method = 'cost+comparables';
    reasons.push(`積算 ${man(costApproach)} と事例 ${man(compApproach)} を ${Math.round((1 - wComp) * 100)}:${Math.round(wComp * 100)} で合成`);
    const gap = Math.abs(costApproach - compApproach) / mid;
    if (gap > 0.25) {
      spread += Math.min(0.1, (gap - 0.25) / 2);
      reasons.push('積算と事例の差が大きいため推定幅を広げています');
    }
  } else if (costApproach != null) {
    mid = costApproach;
    method = 'cost';
  } else {
    mid = compApproach!;
    method = 'comparables';
  }

  spread = Math.min(0.35, spread);
  const confidence = Math.max(0.1, Math.min(0.9, 1 - spread / 0.4));
  const low = Math.floor((mid * (1 - spread)) / 1_000_000) * 1_000_000;
  const high = Math.ceil((mid * (1 + spread)) / 1_000_000) * 1_000_000;
  return {
    ok: true,
    valuation: {
      estimatedLow: low,
      estimatedMid: Math.round(mid / 100_000) * 100_000,
      estimatedHigh: Math.max(high, low + 1_000_000),
      confidence: Math.round(confidence * 100) / 100,
      confidenceLabel: confidence >= 0.7 ? '高' : confidence >= 0.45 ? '中' : '低',
      reasons,
      components: {
        landUnitPriceYenPerM2: landUnit == null ? null : Math.round(landUnit),
        landValueYen: landValue == null ? null : Math.round(landValue),
        buildingValueYen: buildingValue == null ? null : Math.round(buildingValue),
        costApproachYen: costApproach == null ? null : Math.round(costApproach),
        comparableApproachYen: compApproach == null ? null : Math.round(compApproach),
      },
      method,
    },
  };
}
