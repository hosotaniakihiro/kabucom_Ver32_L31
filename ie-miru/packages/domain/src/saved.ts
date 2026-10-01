/** 保存した家 */
export const SAVED_STATUSES = {
  interested: '気になる',
  own: '自宅（所有）',
  family_home: '実家',
  selling: '売却検討中',
  renovating: 'リフォーム中',
  watching: 'ウォッチ中',
} as const;

export type SavedStatus = keyof typeof SAVED_STATUSES;

export interface SavedBuilding {
  buildingId: string;
  nickname: string;
  status: SavedStatus;
  /** 再表示用の最小限のスナップショット（重心・用途など。個人情報は含めない） */
  snapshot: { lat: number; lng: number; usage: string | null; sourceMode: string } | null;
  createdAt: string;
  updatedAt: string;
}

export function validateSaved(input: Record<string, unknown>): { ok: true; value: Pick<SavedBuilding, 'buildingId' | 'nickname' | 'status' | 'snapshot'> } | { ok: false; errors: string[] } {
  const errors: string[] = [];
  const buildingId = typeof input.buildingId === 'string' && input.buildingId && input.buildingId.length <= 200 ? input.buildingId : null;
  if (!buildingId) errors.push('buildingId が不正です');
  const status = typeof input.status === 'string' && input.status in SAVED_STATUSES ? (input.status as SavedStatus) : input.status === undefined ? 'interested' : null;
  if (!status) errors.push('status が不正です');
  const nickname = typeof input.nickname === 'string' ? input.nickname.trim().slice(0, 60) : '';
  let snapshot: SavedBuilding['snapshot'] = null;
  const s = input.snapshot as Record<string, unknown> | undefined;
  if (s && typeof s.lat === 'number' && typeof s.lng === 'number') {
    snapshot = { lat: s.lat, lng: s.lng, usage: typeof s.usage === 'string' ? s.usage.slice(0, 40) : null, sourceMode: typeof s.sourceMode === 'string' ? s.sourceMode.slice(0, 10) : 'live' };
  }
  if (errors.length) return { ok: false, errors };
  return { ok: true, value: { buildingId: buildingId!, nickname: nickname || '保存した家', status: status!, snapshot } };
}
