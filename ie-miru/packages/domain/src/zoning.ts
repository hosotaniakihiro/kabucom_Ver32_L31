/**
 * 用途地域の参考ロジック（建築基準法の概略）。最終判断は自治体・建築士による確認が必要。
 * ここで出す値はすべて「参考情報」であり「建築可能」と断定しない。
 */

export const USE_DISTRICTS = [
  '第一種低層住居専用地域',
  '第二種低層住居専用地域',
  '田園住居地域',
  '第一種中高層住居専用地域',
  '第二種中高層住居専用地域',
  '第一種住居地域',
  '第二種住居地域',
  '準住居地域',
  '近隣商業地域',
  '商業地域',
  '準工業地域',
  '工業地域',
  '工業専用地域',
] as const;

export type UseDistrict = (typeof USE_DISTRICTS)[number];

export type UseDistrictGroup = 'low_rise_residential' | 'residential' | 'commercial' | 'industrial' | 'unknown';

/** 表記ゆれ（「一低層」「1低専」「第1種低層住居専用地域」等）を正式名称へ */
export function normalizeUseDistrict(s: string | null | undefined): UseDistrict | null {
  if (!s) return null;
  const t = String(s)
    .replace(/[１-９]/g, (c) => String('１２３４５６７８９'.indexOf(c) + 1))
    .replace(/第1種/g, '第一種')
    .replace(/第2種/g, '第二種')
    .replace(/\s/g, '');
  const exact = USE_DISTRICTS.find((d) => t.includes(d));
  if (exact) return exact;
  const abbrev: Array<[RegExp, UseDistrict]> = [
    [/(一|1)(種)?低(層|専)/, '第一種低層住居専用地域'],
    [/(二|2)(種)?低(層|専)/, '第二種低層住居専用地域'],
    [/田園/, '田園住居地域'],
    [/(一|1)(種)?中(高|専)/, '第一種中高層住居専用地域'],
    [/(二|2)(種)?中(高|専)/, '第二種中高層住居専用地域'],
    [/準住/, '準住居地域'],
    [/(一|1)(種)?住/, '第一種住居地域'],
    [/(二|2)(種)?住/, '第二種住居地域'],
    [/近(隣)?商/, '近隣商業地域'],
    [/準工/, '準工業地域'],
    [/工専|工業専用/, '工業専用地域'],
    [/商業/, '商業地域'],
    [/工業/, '工業地域'],
  ];
  for (const [re, d] of abbrev) if (re.test(t)) return d;
  return null;
}

export function useDistrictGroup(d: UseDistrict | null): UseDistrictGroup {
  if (!d) return 'unknown';
  if (d === '第一種低層住居専用地域' || d === '第二種低層住居専用地域' || d === '田園住居地域') return 'low_rise_residential';
  if (d.includes('住居')) return 'residential';
  if (d.includes('商業')) return 'commercial';
  return 'industrial';
}

/** 低層住居系の絶対高さ制限（10m または 12m。都市計画で定める）。他は null（道路斜線等は別途） */
export function absoluteHeightLimitM(d: UseDistrict | null): { minM: number; maxM: number } | null {
  return useDistrictGroup(d) === 'low_rise_residential' ? { minM: 10, maxM: 12 } : null;
}

/**
 * 前面道路幅員による容積率の上限（幅員12m未満の場合）。
 * 住居系: 幅員×0.4、その他: 幅員×0.6（特定行政庁の指定で異なる場合がある）。
 */
export function roadLimitedFarPct(d: UseDistrict | null, roadWidthM: number | null): number | null {
  if (roadWidthM == null || roadWidthM <= 0 || roadWidthM >= 12) return null;
  const g = useDistrictGroup(d);
  const k = g === 'low_rise_residential' || g === 'residential' ? 0.4 : 0.6;
  return Math.round(roadWidthM * k * 100);
}

/** 指定容積率と道路幅員制限の小さい方 */
export function effectiveFarPct(d: UseDistrict | null, designatedFarPct: number | null, roadWidthM: number | null): number | null {
  const road = roadLimitedFarPct(d, roadWidthM);
  if (designatedFarPct == null) return road;
  return road == null ? designatedFarPct : Math.min(designatedFarPct, road);
}
