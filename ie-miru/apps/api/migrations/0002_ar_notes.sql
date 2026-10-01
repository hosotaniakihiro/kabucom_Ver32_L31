-- Phase 15: AR修繕メモ（再配置方式のアンカー情報を JSON で保持）。冪等。
CREATE TABLE IF NOT EXISTS ar_notes (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  building_id TEXT NOT NULL,
  status TEXT NOT NULL,
  text TEXT NOT NULL DEFAULT '',
  found_on TEXT NOT NULL,
  anchor TEXT NOT NULL,
  world_map_key TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ar_notes_device_building ON ar_notes (device_id, building_id, updated_at);
