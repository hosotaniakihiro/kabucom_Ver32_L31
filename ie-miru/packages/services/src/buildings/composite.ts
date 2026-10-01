import type { Building, LatLng, SourceRef } from '@ie-miru/domain';
import type { BuildingQueryResult, BuildingSource } from './types';

/**
 * 複数ソースを優先順に試す。先頭が失敗または 0 件なら次へ（例: PLATEAU → OSM → デモ）。
 * id の名前空間（plateau: / osm: / demo:）で getById の委譲先を決める。
 */
export class CompositeBuildingSource implements BuildingSource {
  readonly id = 'composite';
  constructor(private readonly sources: BuildingSource[]) {}

  async findNear(center: LatLng, radiusM: number, signal?: AbortSignal): Promise<BuildingQueryResult> {
    const tried: SourceRef[] = [];
    const errors: string[] = [];
    for (const s of this.sources) {
      const r = await s.findNear(center, radiusM, signal);
      tried.push(...r.sources);
      if (r.status === 'ok' && r.buildings.length > 0) return { ...r, sources: r.sources };
      if (r.status === 'error') errors.push(`${s.id}: ${r.error}`);
    }
    if (errors.length === this.sources.length) return { buildings: [], sources: tried, status: 'error', error: errors.join('; ') };
    return { buildings: [], sources: tried, status: 'ok' };
  }

  async getById(id: string, hint?: LatLng): Promise<Building | null> {
    const ns = id.split(':')[0];
    for (const s of this.sources) {
      if (s.id === ns || ns === undefined) {
        const b = await s.getById(id, hint);
        if (b) return b;
      }
    }
    return null;
  }
}
