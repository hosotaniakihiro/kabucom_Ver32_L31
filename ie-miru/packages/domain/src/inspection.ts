/** 「直す」: 修繕箇所の写真記録 */

export const INSPECTION_CATEGORIES = {
  exterior_wall: '外壁',
  roof: '屋根',
  gutter: '雨樋',
  window: '窓',
  entrance: '玄関',
  fence: '塀',
  other: 'その他',
} as const;

export type InspectionCategory = keyof typeof INSPECTION_CATEGORIES;

/** 写真上のマーキング（画像サイズに依存しない 0..1 の正規化座標） */
export interface PhotoMark {
  x: number;
  y: number;
  /** 円の半径（画像幅に対する比率） */
  r: number;
  label: string | null;
}

/** LiDAR 等で測った実寸（あれば） */
export interface Measurement {
  kind: 'length' | 'area' | 'distance';
  value: number;
  unit: 'm' | 'm2';
  method: 'lidar' | 'ar_plane' | 'manual';
}

export interface Inspection {
  id: string;
  buildingId: string;
  category: InspectionCategory;
  photoKey: string | null;
  marks: PhotoMark[];
  memo: string;
  measurements: Measurement[];
  createdAt: string;
}

export interface InspectionInput {
  buildingId: unknown;
  category: unknown;
  marks: unknown;
  memo: unknown;
  measurements?: unknown;
}

export type Validated<T> = { ok: true; value: T } | { ok: false; errors: string[] };

const clamp01 = (v: number) => Math.min(1, Math.max(0, v));

export function parseMarks(v: unknown): PhotoMark[] | null {
  if (v == null) return [];
  if (!Array.isArray(v) || v.length > 50) return null;
  const out: PhotoMark[] = [];
  for (const m of v) {
    if (!m || typeof m !== 'object') return null;
    const o = m as Record<string, unknown>;
    if (typeof o.x !== 'number' || typeof o.y !== 'number' || !Number.isFinite(o.x) || !Number.isFinite(o.y)) return null;
    out.push({ x: clamp01(o.x), y: clamp01(o.y), r: typeof o.r === 'number' && Number.isFinite(o.r) ? Math.min(0.5, Math.max(0.005, o.r)) : 0.04, label: typeof o.label === 'string' ? o.label.slice(0, 40) : null });
  }
  return out;
}

export function parseMeasurements(v: unknown): Measurement[] {
  if (!Array.isArray(v)) return [];
  return v
    .filter((m): m is Record<string, unknown> => !!m && typeof m === 'object')
    .filter((m) => ['length', 'area', 'distance'].includes(String(m.kind)) && typeof m.value === 'number' && Number.isFinite(m.value) && (m.value as number) >= 0)
    .slice(0, 20)
    .map((m) => ({
      kind: m.kind as Measurement['kind'],
      value: Math.round((m.value as number) * 1000) / 1000,
      unit: m.kind === 'area' ? 'm2' : 'm',
      method: (['lidar', 'ar_plane', 'manual'].includes(String(m.method)) ? m.method : 'manual') as Measurement['method'],
    }));
}

export function validateInspection(input: InspectionInput): Validated<Omit<Inspection, 'id' | 'photoKey' | 'createdAt'>> {
  const errors: string[] = [];
  const buildingId = typeof input.buildingId === 'string' && input.buildingId.length > 0 && input.buildingId.length <= 200 ? input.buildingId : null;
  if (!buildingId) errors.push('buildingId が不正です');
  const category = typeof input.category === 'string' && input.category in INSPECTION_CATEGORIES ? (input.category as InspectionCategory) : null;
  if (!category) errors.push('category が不正です');
  const marks = parseMarks(input.marks);
  if (!marks) errors.push('marks が不正です');
  const memo = typeof input.memo === 'string' ? input.memo.slice(0, 2000) : '';
  if (errors.length) return { ok: false, errors };
  return { ok: true, value: { buildingId: buildingId!, category: category!, marks: marks!, memo, measurements: parseMeasurements(input.measurements) } };
}

export const ALLOWED_PHOTO_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif'];
export const MAX_PHOTO_BYTES = 10 * 1024 * 1024;
