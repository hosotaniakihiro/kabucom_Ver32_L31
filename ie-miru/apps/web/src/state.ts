import type { Building } from '@ie-miru/domain';

/** 画面間で共有する軽い状態（直近に見た建物など） */
export const appState = {
  buildings: new Map<string, Building>(),
  lastPosition: null as { lat: number; lng: number } | null,
  remember(list: Building[]) {
    for (const b of list) this.buildings.set(b.id, b);
  },
};
