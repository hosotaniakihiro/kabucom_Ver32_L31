import type { SourceRef } from './provenance';

export type HazardType = 'flood' | 'inland_flood' | 'tsunami' | 'storm_surge' | 'landslide' | 'liquefaction';

export const HAZARD_TYPES: HazardType[] = ['flood', 'inland_flood', 'tsunami', 'storm_surge', 'landslide', 'liquefaction'];

export const HAZARD_LABEL: Record<HazardType, string> = {
  flood: '洪水',
  inland_flood: '内水',
  tsunami: '津波',
  storm_surge: '高潮',
  landslide: '土砂災害',
  liquefaction: '液状化',
};

/**
 * ハザードの状態。**「安全」「区域外」を表す状態は意図的に存在しない。**
 * データに区域が無いことは「安全」を意味しない（未整備・想定外の災害があり得る）。
 *
 * - in_zone:        想定区域・警戒区域の中にある（level に深さ・区分）
 * - graded:         区域ではなく段階評価のデータがある（例: 液状化の発生傾向）
 * - no_data:        データを確認したが該当する区域が登録されていない（データなし）
 * - not_applicable: 地理的に対象外（例: 海に面していない県の津波・高潮）（対象外）
 * - unavailable:    取得失敗・データソース未接続（確認できず）
 */
export type HazardStatus = 'in_zone' | 'graded' | 'no_data' | 'not_applicable' | 'unavailable';

export interface HazardResult {
  type: HazardType;
  status: HazardStatus;
  /** 例: 「0.5m以上3.0m未満」「土砂災害特別警戒区域（急傾斜地の崩壊）」 */
  level: string | null;
  /** 1(低)〜4(高)。in_zone/graded で推定できた場合のみ */
  severity: 1 | 2 | 3 | 4 | null;
  /** 補足（河川名など） */
  detail: string | null;
  reason: string | null;
  sources: SourceRef[];
}

/** 海に面していない都道府県（JIS コード） */
export const LANDLOCKED_PREFECTURES = new Set(['09', '10', '11', '19', '20', '21', '25', '29']);

export function isCoastalHazard(t: HazardType): boolean {
  return t === 'tsunami' || t === 'storm_surge';
}

/** 浸水深の文字列から重大度を推定（最大値で判定） */
export function severityFromDepth(level: string | null): 1 | 2 | 3 | 4 | null {
  if (!level) return null;
  const nums = [...level.matchAll(/(\d+(?:\.\d+)?)\s*m/g)].map((m) => Number(m[1]));
  if (nums.length === 0) return null;
  const isUpperOpen = /以上\s*$/.test(level.trim()) || (/以上/.test(level) && !/未満/.test(level));
  const hi = Math.max(...nums);
  const v = isUpperOpen ? hi + 0.01 : hi;
  if (v <= 0.5) return 1;
  if (v <= 3) return 2;
  if (v <= 5) return 3;
  return 4;
}

/** 利用者向けの状態ラベル。どの状態からも「安全」という語は生成しない。 */
export function hazardStatusLabel(r: Pick<HazardResult, 'status' | 'level'>): string {
  switch (r.status) {
    case 'in_zone':
      return r.level ? `想定区域内（${r.level}）` : '想定区域内';
    case 'graded':
      return r.level ?? '評価あり';
    case 'no_data':
      return 'データなし';
    case 'not_applicable':
      return '対象外';
    case 'unavailable':
      return '確認できず';
  }
}

/** 補足説明（データなしの意味を誤解させないため必ず添える） */
export function hazardStatusExplanation(r: Pick<HazardResult, 'status' | 'reason'>): string {
  switch (r.status) {
    case 'in_zone':
      return '公的な想定区域に含まれています。自治体のハザードマップで詳細を確認してください。';
    case 'graded':
      return '段階評価の参考値です。地盤調査の代わりにはなりません。';
    case 'no_data':
      return '想定区域の登録が見つかりませんでした。区域外とは限らず、安全を意味するものではありません。';
    case 'not_applicable':
      return r.reason ?? '地理的条件により対象外です。';
    case 'unavailable':
      return r.reason ?? 'データを取得できませんでした。自治体のハザードマップを確認してください。';
  }
}

export interface HazardSummary {
  inZone: HazardType[];
  unknown: HazardType[];
  headline: string;
}

export function summarizeHazards(results: HazardResult[]): HazardSummary {
  const inZone = results.filter((r) => r.status === 'in_zone').map((r) => r.type);
  const unknown = results.filter((r) => r.status === 'unavailable').map((r) => r.type);
  let headline: string;
  if (inZone.length > 0) headline = `想定区域内: ${inZone.map((t) => HAZARD_LABEL[t]).join('・')}`;
  else if (unknown.length === results.length) headline = '災害リスクを確認できませんでした';
  else headline = '想定区域の登録は見つかりませんでした（安全を意味しません）';
  if (inZone.length > 0 && unknown.length > 0) headline += ` ／ 確認できず: ${unknown.map((t) => HAZARD_LABEL[t]).join('・')}`;
  return { inZone, unknown, headline };
}
