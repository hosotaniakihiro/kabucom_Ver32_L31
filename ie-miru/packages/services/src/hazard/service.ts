import {
  HAZARD_TYPES, LANDLOCKED_PREFECTURES, isCoastalHazard, lngLatToTile, severityFromDepth,
  type HazardResult, type HazardType, type LatLng, type SourceRef,
} from '@ie-miru/domain';
import type { GeoJsonFC, ReinfolibClient, TileApi } from '../reinfolib/client';
import { featuresContaining, pickProp } from '../reinfolib/parse';
import { reinfolibSource } from '../reinfolib/service';

/** ハザード種別 → 不動産情報ライブラリ API。内水は全国一律の公開APIが無いため未接続。 */
export const HAZARD_API: Record<HazardType, TileApi | null> = {
  flood: 'XKT026',
  inland_flood: null,
  tsunami: 'XKT028',
  storm_surge: 'XKT027',
  landslide: 'XKT029',
  liquefaction: 'XKT025',
};

const LANDSLIDE_PHENOMENON: Record<string, string> = { '1': '急傾斜地の崩壊', '2': '土石流', '3': '地すべり' };
const LANDSLIDE_ZONE: Record<string, string> = { '1': '土砂災害警戒区域', '2': '土砂災害特別警戒区域' };

const str = (v: unknown) => (v == null || v === '' ? null : String(v));

/** 地点を含む feature の属性から、区域内の説明を作る */
export function interpretHazardFeature(type: HazardType, props: Record<string, unknown>): Pick<HazardResult, 'status' | 'level' | 'severity' | 'detail'> {
  if (type === 'landslide') {
    const ph = LANDSLIDE_PHENOMENON[String(pickProp(props, ['A33_001', 'phenomenon', '現象の種類']) ?? '')] ?? null;
    const zoneCode = String(pickProp(props, ['A33_002', 'zone_class', '区域区分']) ?? '');
    const zone = LANDSLIDE_ZONE[zoneCode] ?? '土砂災害警戒区域等';
    return { status: 'in_zone', level: ph ? `${zone}（${ph}）` : zone, severity: zoneCode === '2' ? 4 : 3, detail: null };
  }
  if (type === 'liquefaction') {
    const t = str(pickProp(props, ['liquefaction_tendency_level', 'tendency', '液状化発生傾向', 'level']));
    if (!t) return { status: 'graded', level: '液状化の発生傾向: 区分不明', severity: null, detail: null };
    const sev = /非常に(しやすい|高)/.test(t) ? 4 : /しやすい|高/.test(t) ? 3 : /しにくい|低/.test(t) ? 1 : 2;
    return { status: 'graded', level: `液状化の発生傾向: ${t}`, severity: sev as 1 | 2 | 3 | 4, detail: null };
  }
  // 浸水系: 深さランクの文字列を探す（属性名は年度で変わるため、値の形で拾う）
  const depthKeys = ['A31a_205', 'A31a_105', 'A49_003', 'A40_003', 'depth', 'rank', '浸水深', '浸水深ランク'];
  let level = str(pickProp(props, depthKeys));
  if (!level) {
    level = Object.values(props).map(String).find((v) => /\d+(\.\d+)?\s*m/.test(v)) ?? null;
  }
  const river = str(pickProp(props, ['river_name', 'A31a_101', '河川名']));
  return { status: 'in_zone', level, severity: severityFromDepth(level), detail: river ? `対象河川: ${river}` : null };
}

/** 6種のハザードを並列に判定する。どれかが失敗しても他は返す。 */
export class HazardService {
  constructor(private readonly client: ReinfolibClient) {}

  async check(p: LatLng, opts: { prefectureCode?: string | null } = {}, signal?: AbortSignal): Promise<HazardResult[]> {
    const tile = lngLatToTile(p, 15);
    return Promise.all(HAZARD_TYPES.map((t) => this.checkOne(t, p, tile, opts.prefectureCode ?? null, signal)));
  }

  private async checkOne(type: HazardType, p: LatLng, tile: { z: number; x: number; y: number }, pref: string | null, signal?: AbortSignal): Promise<HazardResult> {
    const api = HAZARD_API[type];
    const base = { type, level: null, severity: null, detail: null } as const;
    if (pref && isCoastalHazard(type) && LANDLOCKED_PREFECTURES.has(pref)) {
      return { ...base, status: 'not_applicable', reason: '海に面していない都道府県のため対象外です。', sources: [] };
    }
    if (!api) {
      return {
        ...base,
        status: 'unavailable',
        reason: '内水氾濫は全国共通の公開データが未接続です。自治体の内水ハザードマップを確認してください。',
        sources: [],
      };
    }
    const sources: SourceRef[] = [reinfolibSource(api, this.client.mode)];
    let fc: GeoJsonFC;
    try {
      fc = await this.client.tile(api, tile, {}, signal);
    } catch (e) {
      return { ...base, status: 'unavailable', reason: `取得に失敗しました（${(e as { kind?: string }).kind ?? 'error'}）`, sources };
    }
    const hit = featuresContaining(fc, p);
    if (hit.length === 0) {
      return { ...base, status: 'no_data', reason: '想定区域の登録が見つかりませんでした（区域外または未整備）。', sources };
    }
    // 複数区域に該当する場合は最も重いもの
    const interpreted = hit.map((f) => interpretHazardFeature(type, f.properties ?? {}));
    interpreted.sort((a, b) => (b.severity ?? 0) - (a.severity ?? 0));
    return { type, ...interpreted[0]!, reason: null, sources };
  }
}
