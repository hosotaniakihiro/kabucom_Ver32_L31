import { requestJson, UpstreamError, type FetchLike } from '../http';

/** 不動産情報ライブラリ API 識別子 */
export const REINFOLIB_APIS = {
  XIT001: '不動産価格（取引価格・成約価格）情報取得API',
  XPT002: '地価公示・地価調査のポイント（点）API',
  XKT002: '都市計画決定GIS（用途地域）API',
  XKT014: '都市計画決定GIS（防火・準防火地域）API',
  XKT016: '災害危険区域API',
  XKT025: '液状化の発生傾向図API',
  XKT026: '洪水浸水想定区域（想定最大規模）API',
  XKT027: '高潮浸水想定区域API',
  XKT028: '津波浸水想定API',
  XKT029: '土砂災害警戒区域API',
} as const;

export type ReinfolibApi = keyof typeof REINFOLIB_APIS;
export type TileApi = Exclude<ReinfolibApi, 'XIT001'>;

export interface GeoJsonFeature {
  type: 'Feature';
  properties: Record<string, unknown> | null;
  geometry: { type: string; coordinates: unknown } | null;
}
export interface GeoJsonFC {
  type: 'FeatureCollection';
  features: GeoJsonFeature[];
}

export interface Xit001Params {
  year: number;
  quarter?: 1 | 2 | 3 | 4;
  /** 市区町村コード（5桁） */
  city?: string;
  /** 都道府県コード（2桁） */
  area?: string;
  /** 01: 取引価格, 02: 成約価格。未指定は両方 */
  priceClassification?: '01' | '02';
}

export interface ReinfolibClient {
  readonly mode: 'live' | 'mock';
  transactions(p: Xit001Params, signal?: AbortSignal): Promise<Record<string, unknown>[]>;
  tile(api: TileApi, tile: { z: number; x: number; y: number }, extra?: Record<string, string | number>, signal?: AbortSignal): Promise<GeoJsonFC>;
}

export const REINFOLIB_BASE_URL = 'https://www.reinfolib.mlit.go.jp/ex-api/external';

/**
 * 本番クライアント。APIキーは `Ocp-Apim-Subscription-Key` ヘッダで送る（URL に載せない）。
 * キーは環境変数/Secret（REINFOLIB_API_KEY）からのみ受け取り、コードに書かない。
 */
export class LiveReinfolibClient implements ReinfolibClient {
  readonly mode = 'live' as const;
  constructor(private readonly opts: { apiKey: string; baseUrl?: string; fetch?: FetchLike; timeoutMs?: number }) {
    if (!opts.apiKey) throw new UpstreamError('not_configured', 'REINFOLIB_API_KEY is not set');
  }

  private url(api: ReinfolibApi, params: Record<string, string | number | undefined>): string {
    const qs = Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== '')
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
      .join('&');
    return `${this.opts.baseUrl ?? REINFOLIB_BASE_URL}/${api}?${qs}`;
  }

  private get<T>(url: string, api: ReinfolibApi, signal?: AbortSignal) {
    return requestJson<T>(url, {
      fetch: this.opts.fetch,
      timeoutMs: this.opts.timeoutMs,
      headers: { 'Ocp-Apim-Subscription-Key': this.opts.apiKey },
      sourceId: `reinfolib.${api}`,
      signal,
    });
  }

  async transactions(p: Xit001Params, signal?: AbortSignal) {
    const json = await this.get<{ status?: string; data?: Record<string, unknown>[] }>(
      this.url('XIT001', { year: p.year, quarter: p.quarter, city: p.city, area: p.area, priceClassification: p.priceClassification }),
      'XIT001',
      signal,
    );
    return Array.isArray(json?.data) ? json!.data : [];
  }

  async tile(api: TileApi, t: { z: number; x: number; y: number }, extra: Record<string, string | number> = {}, signal?: AbortSignal) {
    const json = await this.get<GeoJsonFC>(this.url(api, { response_format: 'geojson', z: t.z, x: t.x, y: t.y, ...extra }), api, signal);
    return json && Array.isArray(json.features) ? json : { type: 'FeatureCollection' as const, features: [] };
  }
}
