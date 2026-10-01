import {
  available, distanceM, lngLatToTile, missing, tilesCovering,
  type LandPricePoint, type LatLng, type Sourced, type SourceRef, type Transaction, type ZoningInfo,
} from '@ie-miru/domain';
import { REINFOLIB_APIS, type ReinfolibApi, type ReinfolibClient } from './client';
import { parseLandPrices, parseTransactions, parseZoningAt } from './parse';

export function reinfolibSource(api: ReinfolibApi, mode: 'live' | 'mock'): SourceRef {
  return {
    id: `reinfolib.${api}`,
    name: `国土交通省 不動産情報ライブラリ（${REINFOLIB_APIS[api]}）${mode === 'mock' ? '【モック】' : ''}`,
    url: 'https://www.reinfolib.mlit.go.jp/',
    license: '出典: 国土交通省 不動産情報ライブラリ（利用規約に従い加工）',
    mode,
    fetchedAt: new Date().toISOString(),
  };
}

const errReason = (e: unknown) => `取得に失敗しました（${(e as { kind?: string })?.kind ?? (e as Error)?.message ?? 'error'}）`;

/** 不動産情報ライブラリのドメイン向けファサード。失敗は例外にせず Sourced の unavailable で返す。 */
export class RealEstateDataService {
  constructor(private readonly client: ReinfolibClient, private readonly now: () => Date = () => new Date()) {}

  get mode() {
    return this.client.mode;
  }

  async zoning(p: LatLng, signal?: AbortSignal): Promise<Sourced<ZoningInfo>> {
    const t = lngLatToTile(p, 15);
    const src = [reinfolibSource('XKT002', this.client.mode)];
    try {
      const [fc, fire] = await Promise.all([
        this.client.tile('XKT002', t, {}, signal),
        this.client.tile('XKT014', t, {}, signal).catch(() => undefined),
      ]);
      if (fire) src.push(reinfolibSource('XKT014', this.client.mode));
      const z = parseZoningAt(fc, p, fire);
      // 用途地域の指定がない区域（市街化調整区域・都市計画区域外など）もあり得る
      return z ? available(z, 'public', src) : missing('no_data', 'public', src, '用途地域の指定データが見つかりません（指定なし、または未整備の可能性）');
    } catch (e) {
      return missing('unavailable', 'public', src, errReason(e));
    }
  }

  async landPrices(p: LatLng, opts: { radiusM?: number; limit?: number } = {}, signal?: AbortSignal): Promise<Sourced<LandPricePoint[]>> {
    const radius = opts.radiusM ?? 2000;
    const src = [reinfolibSource('XPT002', this.client.mode)];
    const thisYear = this.now().getFullYear();
    try {
      for (const year of [thisYear, thisYear - 1, thisYear - 2]) {
        const tiles = tilesCovering(p, radius, 13);
        const fcs = await Promise.all(tiles.map((t) => this.client.tile('XPT002', t, { year }, signal)));
        const pts = fcs
          .flatMap(parseLandPrices)
          .map((pt) => ({ ...pt, distanceM: Math.round(distanceM(p, pt.location)) }))
          .filter((pt) => pt.distanceM <= radius)
          .sort((a, b) => a.distanceM - b.distanceM);
        const uniq = [...new Map(pts.map((x) => [x.id, x])).values()].slice(0, opts.limit ?? 5);
        if (uniq.length > 0) return available(uniq, 'public', src);
      }
      return missing('no_data', 'public', src, `半径${radius / 1000}km以内に地価公示・地価調査の地点がありません`);
    } catch (e) {
      return missing('unavailable', 'public', src, errReason(e));
    }
  }

  async transactions(cityCode: string | null, opts: { years?: number } = {}, signal?: AbortSignal): Promise<Sourced<Transaction[]>> {
    const src = [reinfolibSource('XIT001', this.client.mode)];
    if (!cityCode) return missing('unavailable', 'public', src, '市区町村を特定できなかったため取引事例を取得できません');
    const y = this.now().getFullYear();
    const years = Array.from({ length: opts.years ?? 2 }, (_, i) => y - 1 - i);
    try {
      const rows = (await Promise.all(years.map((year) => this.client.transactions({ year, city: cityCode }, signal)))).flat();
      const tx = parseTransactions(rows);
      return tx.length > 0 ? available(tx, 'public', src) : missing('no_data', 'public', src, '直近の取引事例がありません');
    } catch (e) {
      return missing('unavailable', 'public', src, errReason(e));
    }
  }
}
