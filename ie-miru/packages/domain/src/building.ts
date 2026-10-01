import { polygonAreaM2, polygonCentroid, type LatLng, type Ring } from './geo';
import type { SourceRef } from './provenance';

/**
 * 建物。どのデータソース由来でもこの形に正規化する（PLATEAU 固有の属性名を本体ロジックに持ち込まない）。
 * 地域・データによって属性が無いのは正常。欠損は null（0 や空文字にしない）。
 */
export interface Building {
  /** 名前空間付きID: `plateau:<gml_id>` / `osm:way/123` / `demo:...` */
  id: string;
  source: SourceRef;
  footprint: Ring;
  centroid: LatLng;
  /** 建物の投影面積 [m²]（footprint から計算。推定値） */
  footprintAreaM2: number;
  heightM: number | null;
  floorsAbove: number | null;
  floorsBelow: number | null;
  /** 用途（日本語ラベル） */
  usage: string | null;
  usageCode: string | null;
  /** 構造（日本語ラベル） */
  structure: string | null;
  structureCode: string | null;
  /** 建築年（西暦）。PLATEAU の yearOfConstruction 等。多くの地域で欠損 */
  builtYear: number | null;
  /** 建物名（公開されている名称のみ。個人名は入れない） */
  name: string | null;
  /** LOD（PLATEAU）。不明なら null */
  lod: number | null;
}

export type BuildingInit = Omit<Building, 'centroid' | 'footprintAreaM2'> & Partial<Pick<Building, 'centroid' | 'footprintAreaM2'>>;

export function makeBuilding(init: BuildingInit): Building {
  return {
    ...init,
    centroid: init.centroid ?? polygonCentroid(init.footprint),
    footprintAreaM2: init.footprintAreaM2 ?? Math.round(polygonAreaM2(init.footprint) * 10) / 10,
  };
}

/** 構造区分（耐用年数・再調達単価の推定に使う） */
export type StructureClass = 'wood' | 'light_steel' | 'steel' | 'rc' | 'src' | 'masonry' | 'unknown';

export function structureClassOf(label: string | null | undefined): StructureClass {
  if (!label) return 'unknown';
  if (/鉄骨鉄筋|SRC/i.test(label)) return 'src';
  if (/鉄筋コンクリート|RC/i.test(label)) return 'rc';
  if (/軽量鉄骨/.test(label)) return 'light_steel';
  if (/鉄骨|S造|^S$/.test(label)) return 'steel';
  if (/木造|W造|土蔵/.test(label)) return 'wood';
  if (/ブロック|レンガ|石造/.test(label)) return 'masonry';
  return 'unknown';
}

/** 階数が無い場合に高さから推定（1階≒3m）。推定であることを呼び出し側で明示すること。 */
export function estimateFloorsFromHeight(heightM: number | null): number | null {
  if (heightM == null || !Number.isFinite(heightM) || heightM <= 0) return null;
  return Math.max(1, Math.round(heightM / 3.2));
}
