import type { BuildingReport } from '@ie-miru/services';

export interface AnalysisRepo {
  saveReport(r: BuildingReport): Promise<void>;
}

/** 永続化の集約。Phase 18/19 で項目を追加する。 */
export interface Repositories {
  analysis: AnalysisRepo;
}
