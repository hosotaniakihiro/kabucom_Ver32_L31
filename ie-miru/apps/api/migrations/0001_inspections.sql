-- Phase 14: 直す（修繕写真）。冪等（何度流しても同じ結果）。
CREATE TABLE IF NOT EXISTS inspections (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  building_id TEXT NOT NULL,
  category TEXT NOT NULL,
  photo_key TEXT,
  marks TEXT NOT NULL DEFAULT '[]',
  memo TEXT NOT NULL DEFAULT '',
  measurements TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inspections_device_building ON inspections (device_id, building_id, created_at);
