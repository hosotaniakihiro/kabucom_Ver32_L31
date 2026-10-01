-- Phase 19: 建物・出典・分析結果・ハザードのキャッシュ（公的データ由来。利用者データではない）。冪等。
CREATE TABLE IF NOT EXISTS buildings (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  source_mode TEXT NOT NULL,
  centroid_lat REAL NOT NULL,
  centroid_lng REAL NOT NULL,
  footprint TEXT NOT NULL,
  attributes TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_buildings_centroid ON buildings (centroid_lat, centroid_lng);

CREATE TABLE IF NOT EXISTS building_sources (
  building_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_name TEXT NOT NULL,
  mode TEXT NOT NULL,
  url TEXT,
  license TEXT,
  fetched_at TEXT,
  PRIMARY KEY (building_id, source_id)
);

CREATE TABLE IF NOT EXISTS property_analysis (
  building_id TEXT PRIMARY KEY,
  generated_at TEXT NOT NULL,
  data_mode TEXT NOT NULL,
  valuation_low INTEGER,
  valuation_mid INTEGER,
  valuation_high INTEGER,
  valuation_confidence REAL,
  comparables_count INTEGER,
  use_district TEXT,
  coverage_ratio_pct REAL,
  floor_area_ratio_pct REAL,
  report_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hazards (
  building_id TEXT NOT NULL,
  hazard_type TEXT NOT NULL,
  status TEXT NOT NULL,
  level TEXT,
  severity INTEGER,
  source_ids TEXT NOT NULL DEFAULT '[]',
  checked_at TEXT NOT NULL,
  PRIMARY KEY (building_id, hazard_type)
);
