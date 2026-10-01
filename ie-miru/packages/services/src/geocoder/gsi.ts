import type { LatLng, SourceRef } from '@ie-miru/domain';
import { requestJson, type FetchLike } from '../http';

/** 市区町村レベルの所在。番地・建物所有者などの個人に結びつく情報は扱わない。 */
export interface AreaInfo {
  prefectureCode: string | null;
  cityCode: string | null;
  /** 町丁名（例: 丸の内一丁目） */
  townName: string | null;
  source: SourceRef;
}

export interface Geocoder {
  reverse(p: LatLng, signal?: AbortSignal): Promise<AreaInfo | null>;
}

export const GSI_GEOCODER_SOURCE = {
  id: 'gsi.reverse-geocoder',
  name: '国土地理院 逆ジオコーダ',
  url: 'https://maps.gsi.go.jp/development/',
  license: '国土地理院コンテンツ利用規約',
} as const;

/** 国土地理院 逆ジオコーダ（キー不要） */
export class GsiReverseGeocoder implements Geocoder {
  constructor(private readonly opts: { fetch?: FetchLike; timeoutMs?: number; endpoint?: string } = {}) {}
  async reverse(p: LatLng, signal?: AbortSignal): Promise<AreaInfo | null> {
    const url = `${this.opts.endpoint ?? 'https://mreversegeocoder.gsi.go.jp/reverse-geocoder/LonLatToAddress'}?lat=${p.lat}&lon=${p.lng}`;
    const json = await requestJson<{ results?: { muniCd?: string; lv01Nm?: string } }>(url, { fetch: this.opts.fetch, timeoutMs: this.opts.timeoutMs, sourceId: GSI_GEOCODER_SOURCE.id, signal });
    const r = json?.results;
    if (!r?.muniCd) return null;
    // muniCd は先頭0が落ちて4桁になる場合がある
    const city = String(r.muniCd).padStart(5, '0');
    return {
      prefectureCode: city.slice(0, 2),
      cityCode: city,
      townName: r.lv01Nm && r.lv01Nm !== '－' ? r.lv01Nm : null,
      source: { ...GSI_GEOCODER_SOURCE, mode: 'live', fetchedAt: new Date().toISOString() },
    };
  }
}

/** ネットワーク不可の開発環境用 */
export class MockGeocoder implements Geocoder {
  async reverse(): Promise<AreaInfo | null> {
    return {
      prefectureCode: '13',
      cityCode: '13112',
      townName: 'モック一丁目',
      source: { ...GSI_GEOCODER_SOURCE, name: '逆ジオコーダ（モック）', mode: 'mock', fetchedAt: new Date().toISOString() },
    };
  }
}
