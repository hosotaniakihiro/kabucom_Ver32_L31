import type { ArNote, ArNoteStatus, Building, Inspection, SavedBuilding } from '@ie-miru/domain';
import type { BuildingReport } from '@ie-miru/services';

export interface AnalysisRepo {
  /** レポートと構成要素（建物・出典・査定・ハザード）を保存。mock/demo も mode 付きで保存し混同しない */
  saveReport(r: BuildingReport): Promise<void>;
  /** maxAgeMs 以内に作られたレポート（isolate を跨いだキャッシュ） */
  getReport(buildingId: string, maxAgeMs: number, now: Date): Promise<BuildingReport | null>;
  getBuilding(buildingId: string): Promise<Building | null>;
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

export interface SavedRepo {
  /** 既に保存済みなら nickname/status/snapshot を更新（upsert） */
  upsert(deviceId: string, s: Omit<SavedBuilding, 'createdAt' | 'updatedAt'>, now: string): Promise<SavedBuilding>;
  list(deviceId: string): Promise<SavedBuilding[]>;
  get(deviceId: string, buildingId: string): Promise<SavedBuilding | null>;
  delete(deviceId: string, buildingId: string): Promise<boolean>;
}

/** 永続化の集約（D1 + R2）。利用者データはすべて匿名端末ID（device_id）で分離する。 */
export interface Repositories {
  analysis: AnalysisRepo;
  inspections: InspectionRepo;
  arNotes: ArNoteRepo;
  saved: SavedRepo;
}
