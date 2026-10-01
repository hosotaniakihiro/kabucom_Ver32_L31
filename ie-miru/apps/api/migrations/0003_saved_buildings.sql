-- Phase 18: 保存した家。冪等。
CREATE TABLE IF NOT EXISTS saved_buildings (
  device_id TEXT NOT NULL,
  building_id TEXT NOT NULL,
  nickname TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'interested',
  snapshot TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (device_id, building_id)
);
CREATE INDEX IF NOT EXISTS idx_saved_buildings_device_updated ON saved_buildings (device_id, updated_at);
