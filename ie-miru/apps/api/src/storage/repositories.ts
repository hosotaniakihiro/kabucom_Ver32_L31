import type { ArNote, ArNoteStatus, Inspection } from '@ie-miru/domain';
import type { BuildingReport } from '@ie-miru/services';

export interface AnalysisRepo {
  saveReport(r: BuildingReport): Promise<void>;
}

export interface InspectionRepo {
  create(deviceId: string, i: Inspection, photo: { data: Uint8Array; contentType: string } | null): Promise<Inspection>;
  list(deviceId: string, buildingId: string | null): Promise<Inspection[]>;
  get(deviceId: string, id: string): Promise<Inspection | null>;
  photo(deviceId: string, id: string): Promise<{ data: ArrayBuffer; contentType: string } | null>;
  delete(deviceId: string, id: string): Promise<boolean>;
}

export interface ArNoteRepo {
  create(deviceId: string, n: ArNote): Promise<ArNote>;
  list(deviceId: string, buildingId: string | null): Promise<ArNote[]>;
  update(deviceId: string, id: string, patch: { status?: ArNoteStatus; text?: string; updatedAt: string }): Promise<ArNote | null>;
  delete(deviceId: string, id: string): Promise<boolean>;
  /** iOS の ARWorldMap（バイナリ）を R2 に保存 */
  putWorldMap(deviceId: string, id: string, data: Uint8Array): Promise<string | null>;
  getWorldMap(deviceId: string, id: string): Promise<ArrayBuffer | null>;
}

/** 永続化の集約（D1 + R2）。利用者データはすべて匿名端末ID（device_id）で分離する。 */
export interface Repositories {
  analysis: AnalysisRepo;
  inspections: InspectionRepo;
  arNotes: ArNoteRepo;
}
