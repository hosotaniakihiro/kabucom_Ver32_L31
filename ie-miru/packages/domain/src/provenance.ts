/**
 * データの出自と状態。公的データ・推定・ユーザー入力・参考情報を混同しないための型。
 *
 * 最重要ルール: 「データが存在しない」は値 0 ではない。値が無いときは必ず status で表す。
 */

/** UI のバッジ: 公的データ / AI推定 / ユーザー登録 / 参考情報 */
export type ProvenanceKind = 'public' | 'ai_estimate' | 'user' | 'reference';

/** 取得モード。mock は「APIキー未設定などで本物ではない」ことを示し、UI で必ず明示する */
export type DataMode = 'live' | 'mock' | 'demo' | 'cache';

export interface SourceRef {
  /** 機械可読ID 例: reinfolib.XIT001, plateau.mvt, gsi.reverse-geocoder */
  id: string;
  /** 表示名 例: 国土交通省 不動産情報ライブラリ（不動産取引価格情報） */
  name: string;
  url: string | null;
  license: string | null;
  mode: DataMode;
  fetchedAt: string | null;
}

/**
 * - available:      値あり
 * - no_data:        問い合わせたが該当データがない（データなし）
 * - not_applicable: そもそも対象外（例: 内陸県の津波）
 * - unavailable:    取得失敗・未接続などで確認できなかった（確認できず）
 */
export type DataStatus = 'available' | 'no_data' | 'not_applicable' | 'unavailable';

export type Sourced<T> =
  | { status: 'available'; value: T; kind: ProvenanceKind; sources: SourceRef[]; note?: string | null }
  | { status: 'no_data' | 'not_applicable' | 'unavailable'; value: null; kind: ProvenanceKind; sources: SourceRef[]; reason: string };

export function available<T>(value: T, kind: ProvenanceKind, sources: SourceRef[], note: string | null = null): Sourced<T> {
  return { status: 'available', value, kind, sources, note };
}

export function missing<T = never>(
  status: 'no_data' | 'not_applicable' | 'unavailable',
  kind: ProvenanceKind,
  sources: SourceRef[],
  reason: string,
): Sourced<T> {
  return { status, value: null, kind, sources, reason };
}

export const DATA_STATUS_LABEL: Record<Exclude<DataStatus, 'available'>, string> = {
  no_data: 'データなし',
  not_applicable: '対象外',
  unavailable: '確認できず',
};

export const PROVENANCE_LABEL: Record<ProvenanceKind, string> = {
  public: '公的データ',
  ai_estimate: 'AI推定',
  user: 'ユーザー登録',
  reference: '参考情報',
};

export function isMock(sources: SourceRef[]): boolean {
  return sources.some((s) => s.mode === 'mock' || s.mode === 'demo');
}
