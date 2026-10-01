import type { BuildingReport } from '@ie-miru/services';
import type { D1Like, R2Like } from './types';
import type { Repositories } from './repositories';

export function createRepositories(db: D1Like, _photos: R2Like | null): Repositories {
  return {
    analysis: {
      async saveReport(_r: BuildingReport) {
        void db;
      },
    },
  };
}
