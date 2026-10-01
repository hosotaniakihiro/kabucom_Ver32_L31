import { describe, expect, it } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { SqliteD1 } from '../src/storage/sqliteD1';
import { applyMigrations } from '../src/storage/local';
import { createRepositories } from '../src/storage/d1';
import { MemoryR2 } from '../src/storage/fileR2';
import { DEMO_CENTER, json, makeApp, makePersistentApp } from './helpers';

const REQUIRED = ['buildings', 'building_sources', 'property_analysis', 'hazards', 'saved_buildings', 'inspections', 'ar_notes'];

describe('D1 migrations', () => {
  it('create all required tables', async () => {
    const db = new SqliteD1(':memory:');
    await applyMigrations(db);
    const rows = db.db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all() as Array<{ name: string }>;
    expect(rows.map((r) => r.name)).toEqual(expect.arrayContaining(REQUIRED));
  });
  it('are idempotent (re-running changes nothing and keeps data)', async () => {
    const db = new SqliteD1(':memory:');
    await applyMigrations(db);
    db.db.exec("INSERT INTO saved_buildings (device_id, building_id, nickname, status, created_at, updated_at) VALUES ('d', 'b', 'n', 'own', 't', 't')");
    await applyMigrations(db);
    await applyMigrations(db);
    expect((db.db.prepare('SELECT COUNT(*) AS c FROM saved_buildings').get() as { c: number }).c).toBe(1);
  });
  it('are additive only (no DROP / DELETE / destructive ALTER)', () => {
    const dir = resolve(__dirname, '../migrations');
    for (const f of readdirSync(dir)) {
      const sql = readFileSync(resolve(dir, f), 'utf8');
      expect(sql, f).not.toMatch(/\bDROP\b|\bDELETE\b|\bTRUNCATE\b|ALTER\s+TABLE\s+\w+\s+(DROP|RENAME)/i);
      expect(sql, f).toMatch(/IF NOT EXISTS/);
    }
  });
});

describe('analysis persistence', () => {
  it('report is stored with building, sources and hazards, and served after isolate restart', async () => {
    const db = new SqliteD1(':memory:');
    await applyMigrations(db);
    const repos = createRepositories(db, new MemoryR2());
    const app1 = makeApp({ repos });
    const { body: near } = await json(app1.request(`/v1/buildings/nearby?lat=${DEMO_CENTER.lat}&lng=${DEMO_CENTER.lng}&radius=60`));
    const id = near.buildings[0].id;
    const { body: r1 } = await json(app1.request(`/v1/buildings/${encodeURIComponent(id)}/report`));
    const pa = db.db.prepare('SELECT * FROM property_analysis WHERE building_id = ?').get(id) as Record<string, unknown>;
    expect(pa.data_mode).toBe('mixed');
    expect(pa.valuation_low).toBe(r1.valuation.value.estimatedLow);
    expect((db.db.prepare('SELECT COUNT(*) AS c FROM hazards WHERE building_id = ?').get(id) as { c: number }).c).toBe(6);
    const srcModes = (db.db.prepare('SELECT mode FROM building_sources WHERE building_id = ?').all(id) as Array<{ mode: string }>).map((x) => x.mode);
    expect(srcModes).toEqual(expect.arrayContaining(['demo', 'mock']));

    // 新しい isolate（メモリキャッシュ空）でも D1 から同じレポート
    const app2 = makeApp({ repos });
    const { body: r2 } = await json(app2.request(`/v1/buildings/${encodeURIComponent(id)}/report`));
    expect(r2.generatedAt).toBe(r1.generatedAt);
  });

  it('missing persistence does not break read-only flows', async () => {
    const { status } = await json(makeApp().request(`/v1/buildings/nearby?lat=${DEMO_CENTER.lat}&lng=${DEMO_CENTER.lng}`));
    expect(status).toBe(200);
  });

  it('health shows persistence', async () => {
    const { app } = await makePersistentApp();
    expect((await json(app.request('/v1/health'))).body.persistence).toBe(true);
  });
});
