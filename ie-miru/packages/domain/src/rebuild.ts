import { absoluteHeightLimitM, normalizeUseDistrict } from './zoning';

/**
 * 建て替えの簡易ボリュームプラン（参考）。
 * 建ぺい率・容積率・用途地域は「参考情報として併記」するだけで、法規適合や「建築可能」を判定しない。
 * 斜線制限・日影規制・地区計画・条例・接道などは考慮していない。
 */
export type RebuildPreset = 'two_story' | 'three_story' | 'rental_combo';

export const REBUILD_PRESETS: Record<RebuildPreset, { label: string; floors: number; floorHeightM: number; roof: 'gable' | 'flat'; structure: string; costPerM2: number; description: string }> = {
  two_story: { label: '2階建て', floors: 2, floorHeightM: 2.9, roof: 'gable', structure: '木造', costPerM2: 300_000, description: '一般的な木造2階建て住宅' },
  three_story: { label: '3階建て', floors: 3, floorHeightM: 2.9, roof: 'flat', structure: '木造または鉄骨造', costPerM2: 340_000, description: '狭小地向けの3階建て住宅' },
  rental_combo: { label: '賃貸併用', floors: 3, floorHeightM: 3.0, roof: 'flat', structure: '鉄骨造または RC 造', costPerM2: 400_000, description: '1階に賃貸住戸2戸、2〜3階を自宅とする賃貸併用住宅' },
};

export type CheckStatus = 'within' | 'exceeds' | 'unknown';

export interface RebuildCheck {
  key: 'coverage' | 'far' | 'height' | 'zoning';
  label: string;
  status: CheckStatus;
  detail: string;
}

export interface RebuildPlan {
  preset: RebuildPreset;
  label: string;
  description: string;
  floors: number;
  footprintM2: number;
  totalFloorM2: number;
  heightM: number;
  /** 3D 表示用の箱寸法 [m] */
  widthM: number;
  depthM: number;
  roof: 'gable' | 'flat';
  structure: string;
  constructionCostYen: number;
  checks: RebuildCheck[];
  notes: string[];
}

export const REBUILD_DISCLAIMER = '参考表示です。斜線制限・日影規制・地区計画・条例・接道条件などは考慮しておらず、「建築可能」であることを示すものではありません。設計者・行政への確認が必要です。';

export function planRebuild(input: {
  preset: RebuildPreset;
  landAreaM2: number | null;
  coverageRatioPct: number | null;
  floorAreaRatioPct: number | null;
  useDistrict: string | null;
  /** 現況建物の投影面積（敷地面積が不明な場合の代替） */
  existingFootprintM2?: number | null;
}): RebuildPlan {
  const p = REBUILD_PRESETS[input.preset];
  const land = input.landAreaM2;
  const notes: string[] = [REBUILD_DISCLAIMER];
  // 建築面積: 敷地×建ぺい率×0.9（外構・後退の余裕）。不明なら現況の投影面積
  let footprint: number;
  if (land != null && input.coverageRatioPct != null) footprint = land * (input.coverageRatioPct / 100) * 0.9;
  else if (input.existingFootprintM2 != null && input.existingFootprintM2 > 0) {
    footprint = input.existingFootprintM2;
    notes.push('敷地面積または建ぺい率が不明なため、現在の建物の大きさで表示しています。');
  } else {
    footprint = 60;
    notes.push('敷地・建物の大きさが不明なため、標準的な大きさ（60㎡）で表示しています。');
  }
  footprint = Math.max(20, Math.round(footprint * 10) / 10);
  const total = Math.round(footprint * p.floors * 10) / 10;
  const height = Math.round((p.floors * p.floorHeightM + (p.roof === 'gable' ? 1.8 : 0.6)) * 10) / 10;

  const checks: RebuildCheck[] = [];
  if (land != null && input.coverageRatioPct != null) {
    const ratio = (footprint / land) * 100;
    checks.push({ key: 'coverage', label: '建ぺい率', status: ratio <= input.coverageRatioPct ? 'within' : 'exceeds', detail: `計画 ${ratio.toFixed(0)}% ／ 指定 ${input.coverageRatioPct}%` });
  } else checks.push({ key: 'coverage', label: '建ぺい率', status: 'unknown', detail: '敷地面積または建ぺい率のデータがありません' });

  if (land != null && input.floorAreaRatioPct != null) {
    const far = (total / land) * 100;
    checks.push({ key: 'far', label: '容積率', status: far <= input.floorAreaRatioPct ? 'within' : 'exceeds', detail: `計画 ${far.toFixed(0)}% ／ 指定 ${input.floorAreaRatioPct}%（前面道路幅員による制限は未考慮）` });
  } else checks.push({ key: 'far', label: '容積率', status: 'unknown', detail: '敷地面積または容積率のデータがありません' });

  const d = normalizeUseDistrict(input.useDistrict);
  const lim = absoluteHeightLimitM(d);
  if (lim) checks.push({ key: 'height', label: '絶対高さ', status: height <= lim.minM ? 'within' : height <= lim.maxM ? 'unknown' : 'exceeds', detail: `計画 約${height}m ／ ${d}は ${lim.minM}m または ${lim.maxM}m（都市計画で指定）` });
  else checks.push({ key: 'height', label: '高さ', status: 'unknown', detail: '道路斜線・北側斜線・高度地区等は未考慮' });

  checks.push({ key: 'zoning', label: '用途地域', status: 'unknown', detail: d ? `${d}（${input.preset === 'rental_combo' ? '共同住宅・長屋の可否や規模は個別確認' : '住宅の用途・規模の可否は個別確認が必要'}）` : '用途地域のデータがありません' });

  const width = Math.sqrt(footprint * 1.3);
  return {
    preset: input.preset,
    label: p.label,
    description: p.description,
    floors: p.floors,
    footprintM2: footprint,
    totalFloorM2: total,
    heightM: height,
    widthM: Math.round(width * 10) / 10,
    depthM: Math.round((footprint / width) * 10) / 10,
    roof: p.roof,
    structure: p.structure,
    constructionCostYen: Math.round((total * p.costPerM2) / 100_000) * 100_000,
    checks,
    notes,
  };
}

/**
 * 3D モデル提供元の差し替え口。Madori3D / sumai3d 等の既存資産が使える場合はここに実装を足す。
 * 既定は箱＋屋根の簡易ボリューム（apps/web の massing / iOS の RebuildARView）。
 */
export interface MassingModelProvider {
  readonly id: string;
  /** glTF/USDZ などのモデル URL を返す。null なら簡易ボリュームで描画 */
  modelFor(plan: RebuildPlan): Promise<{ url: string; format: 'gltf' | 'usdz' } | null>;
}

export const SIMPLE_MASSING_PROVIDER: MassingModelProvider = { id: 'simple-massing', modelFor: async () => null };
