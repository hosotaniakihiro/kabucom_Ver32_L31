import type { Building, LatLng, SourceRef } from '@ie-miru/domain';

export interface BuildingQueryResult {
  buildings: Building[];
  sources: SourceRef[];
  /** ok: 取得成功（0件含む）/ error: 取得できなかった（= 確認できず） */
  status: 'ok' | 'error';
  error?: string;
}

/**
 * 建物データソースの共通 interface。PLATEAU / OSM / デモ等の固有処理はアダプタ内に閉じる。
 */
export interface BuildingSource {
  readonly id: string;
  findNear(center: LatLng, radiusM: number, signal?: AbortSignal): Promise<BuildingQueryResult>;
  /** id から建物を取得。位置ヒントがあればタイル再取得に使う */
  getById(id: string, hint?: LatLng): Promise<Building | null>;
}
