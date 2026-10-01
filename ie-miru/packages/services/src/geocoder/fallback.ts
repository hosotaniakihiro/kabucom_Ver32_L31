import type { LatLng } from '@ie-miru/domain';
import type { AreaInfo, Geocoder } from './gsi';

/** 先頭から順に試し、最初に得られた結果を返す（失敗は握りつぶして次へ）。 */
export class FallbackGeocoder implements Geocoder {
  constructor(private readonly list: Geocoder[]) {}
  async reverse(p: LatLng, signal?: AbortSignal): Promise<AreaInfo | null> {
    for (const g of this.list) {
      try {
        const r = await g.reverse(p, signal);
        if (r) return r;
      } catch {
        /* next */
      }
    }
    return null;
  }
}
