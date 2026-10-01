import {
  available, estimateFloorsFromHeight, estimateSubject, estimateValue, isMock, missing, summarizeComparables, summarizeHazards,
  type Building, type ComparablesSummary, type PropertySubject, type Valuation, type HazardResult, type HazardSummary, type LandPricePoint, type Sourced, type SourceRef, type Transaction, type ZoningInfo,
} from '@ie-miru/domain';
import type { Geocoder, AreaInfo } from '../geocoder/gsi';
import type { HazardService } from '../hazard/service';
import type { RealEstateDataService } from '../reinfolib/service';

export interface BuildingFacts {
  heightM: Sourced<number>;
  floorsAbove: Sourced<number>;
  builtYear: Sourced<number>;
  usage: Sourced<string>;
  structure: Sourced<string>;
  footprintAreaM2: Sourced<number>;
}

export interface BuildingReport {
  building: Building;
  generatedAt: string;
  area: Sourced<Omit<AreaInfo, 'source'>>;
  facts: BuildingFacts;
  zoning: Sourced<ZoningInfo>;
  landPrices: Sourced<LandPricePoint[]>;
  /** 周辺取引事例（この建物の価格ではない） */
  transactions: Sourced<Transaction[]>;
  /** 対象物件の推定プロファイル（AI推定。利用者が上書き可能） */
  subject: Sourced<PropertySubject>;
  /** 類似度で絞った周辺取引事例と参考相場 */
  comparables: Sourced<ComparablesSummary>;
  /** AI参考査定（レンジ。正式な鑑定ではない） */
  valuation: Sourced<Valuation>;
  hazards: HazardResult[];
  hazardSummary: HazardSummary;
  /** live / mock / demo / mixed */
  dataMode: 'live' | 'mock' | 'demo' | 'mixed';
  disclaimers: string[];
}

export const REPORT_DISCLAIMERS = [
  'AI参考査定は公開データからの機械的な推定で、正式な不動産鑑定・査定ではありません。必ずレンジ（幅）でご覧ください。',
  '周辺取引事例・参考相場は近隣の取引の情報であり、この建物そのものの売買価格ではありません。',
  'ハザード情報で「データなし」は安全を意味しません。自治体のハザードマップも確認してください。',
  '所有者などの個人情報は表示しません。',
];

function buildingFacts(b: Building): BuildingFacts {
  const src = [b.source];
  const nd = (what: string) => missing<never>('no_data', 'public', src, `${what}のデータがありません`);
  const floorsEst = estimateFloorsFromHeight(b.heightM);
  return {
    heightM: b.heightM != null ? available(b.heightM, 'public', src) : nd('高さ'),
    floorsAbove:
      b.floorsAbove != null
        ? available(b.floorsAbove, 'public', src)
        : floorsEst != null
          ? available(floorsEst, 'ai_estimate', src, '高さから推定（1階≒3.2m）')
          : nd('階数'),
    builtYear: b.builtYear != null ? available(b.builtYear, 'public', src) : nd('建築年'),
    usage: b.usage != null ? available(b.usage, 'public', src) : nd('用途'),
    structure: b.structure != null ? available(b.structure, 'public', src) : nd('構造'),
    footprintAreaM2: available(b.footprintAreaM2, 'ai_estimate', src, '建物外形から計算した投影面積（延床面積ではありません）'),
  };
}

export interface ReportDeps {
  realEstate: RealEstateDataService;
  hazards: HazardService;
  geocoder: Geocoder;
  now?: () => Date;
}

/** 建物1件分の情報をまとめる。各セクションは独立に失敗し得る（全体は失敗させない）。 */
export async function buildReport(b: Building, deps: ReportDeps, signal?: AbortSignal): Promise<BuildingReport> {
  const p = b.centroid;
  const areaP = deps.geocoder.reverse(p, signal).catch(() => null);
  const [area, zoning, landPrices, hazards] = await Promise.all([
    areaP,
    deps.realEstate.zoning(p, signal),
    deps.realEstate.landPrices(p, {}, signal),
    areaP.then((a) => deps.hazards.check(p, { prefectureCode: a?.prefectureCode ?? null }, signal)),
  ]);
  const transactions = await deps.realEstate.transactions(area?.cityCode ?? null, {}, signal);

  const areaSourced: Sourced<Omit<AreaInfo, 'source'>> = area
    ? available({ prefectureCode: area.prefectureCode, cityCode: area.cityCode, townName: area.townName }, 'public', [area.source])
    : missing('unavailable', 'public', [], '所在地（市区町村）を特定できませんでした');

  const townName = area?.townName ?? null;
  const subject = estimateSubject(b, zoning.status === 'available' ? zoning.value : null, { townName });
  const subjectSourced = available(subject, 'ai_estimate', [b.source, ...zoning.sources], '建物外形・用途地域から推定。買う/売る画面で修正できます。');
  const now = deps.now?.() ?? new Date();
  let comparables: Sourced<ComparablesSummary>;
  if (transactions.status === 'available') {
    const summary = summarizeComparables(subject, transactions.value, { sinceYear: now.getFullYear() - 3 });
    comparables =
      summary.count > 0
        ? available(summary, 'reference', transactions.sources, '周辺取引事例から算出した参考相場です。')
        : missing('no_data', 'reference', transactions.sources, '条件の近い取引事例が見つかりませんでした');
  } else {
    comparables = missing(transactions.status, 'reference', transactions.sources, transactions.reason);
  }

  const v = estimateValue({
    subject,
    landPrices: landPrices.status === 'available' ? landPrices.value : null,
    comparables: comparables.status === 'available' ? comparables.value : null,
    now,
  });
  const valuationSources = [...landPrices.sources, ...comparables.sources];
  const valuation: Sourced<Valuation> = v.ok
    ? available(v.valuation, 'ai_estimate', valuationSources, '公開データからの機械的な推定です。正式な不動産鑑定ではありません。')
    : missing(landPrices.status === 'unavailable' && comparables.status === 'unavailable' ? 'unavailable' : 'no_data', 'ai_estimate', valuationSources, v.reason);

  const allSources: SourceRef[] = [
    b.source,
    ...areaSourced.sources,
    ...zoning.sources,
    ...landPrices.sources,
    ...transactions.sources,
    ...hazards.flatMap((h) => h.sources),
  ];
  const modes = new Set(allSources.map((s) => s.mode));
  const dataMode = modes.size === 1 ? ([...modes][0] as 'live' | 'mock' | 'demo') : isMock(allSources) ? 'mixed' : 'live';

  return {
    building: b,
    generatedAt: (deps.now?.() ?? new Date()).toISOString(),
    area: areaSourced,
    facts: buildingFacts(b),
    zoning,
    landPrices,
    transactions,
    subject: subjectSourced,
    comparables,
    valuation,
    hazards,
    hazardSummary: summarizeHazards(hazards),
    dataMode,
    disclaimers: REPORT_DISCLAIMERS,
  };
}
