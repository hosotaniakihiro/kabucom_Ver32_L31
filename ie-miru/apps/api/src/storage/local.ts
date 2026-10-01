import { mkdirSync, readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRepositories } from './d1';
import { FileR2 } from './fileR2';
import { SqliteD1 } from './sqliteD1';

const migrationsDir = resolve(dirname(fileURLToPath(import.meta.url)), '../../migrations');

/** migrations/*.sql を番号順に適用（各ファイルは冪等: CREATE ... IF NOT EXISTS） */
export async function applyMigrations(db: SqliteD1) {
  let files: string[] = [];
  try {
    files = readdirSync(migrationsDir).filter((f) => f.endsWith('.sql')).sort();
  } catch {
    return [];
  }
  for (const f of files) db.db.exec(readFileSync(join(migrationsDir, f), 'utf8'));
  return files;
}

export async function createLocalRepositories(dataDir: string) {
  mkdirSync(dataDir, { recursive: true });
  const db = new SqliteD1(join(dataDir, 'ie-miru.sqlite'));
  await applyMigrations(db);
  return createRepositories(db, new FileR2(join(dataDir, 'r2')));
}
