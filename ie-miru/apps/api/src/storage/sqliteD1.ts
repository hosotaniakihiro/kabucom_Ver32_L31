import { DatabaseSync } from 'node:sqlite';
import type { D1Like, D1PreparedStatementLike, D1Result } from './types';

/**
 * node:sqlite を Cloudflare D1 互換 API に見せるシム（ローカル開発・テスト用）。
 * D1 本体と同じ SQLite 方言なので、migrations/*.sql をそのまま検証できる。
 */
export class SqliteD1 implements D1Like {
  readonly db: DatabaseSync;
  constructor(path = ':memory:') {
    this.db = new DatabaseSync(path);
    this.db.exec('PRAGMA foreign_keys = ON;');
  }
  prepare(sql: string): D1PreparedStatementLike {
    return new Stmt(this.db, sql, []);
  }
  async batch(statements: D1PreparedStatementLike[]): Promise<D1Result[]> {
    this.db.exec('BEGIN');
    try {
      const out: D1Result[] = [];
      for (const s of statements) out.push(await s.run());
      this.db.exec('COMMIT');
      return out;
    } catch (e) {
      this.db.exec('ROLLBACK');
      throw e;
    }
  }
  async exec(sql: string) {
    this.db.exec(sql);
    return { count: 1 };
  }
}

type SqlValue = null | number | bigint | string | Uint8Array;

class Stmt implements D1PreparedStatementLike {
  constructor(private readonly db: DatabaseSync, private readonly sql: string, private readonly params: SqlValue[]) {}
  bind(...values: unknown[]): D1PreparedStatementLike {
    return new Stmt(this.db, this.sql, values.map((v) => (v === undefined ? null : typeof v === 'boolean' ? (v ? 1 : 0) : (v as SqlValue))));
  }
  async first<T>(): Promise<T | null> {
    const row = this.db.prepare(this.sql).get(...this.params);
    return (row as T) ?? null;
  }
  async all<T>(): Promise<D1Result<T>> {
    return { results: this.db.prepare(this.sql).all(...this.params) as T[], success: true };
  }
  async run(): Promise<D1Result> {
    const r = this.db.prepare(this.sql).run(...this.params);
    return { results: [], success: true, meta: { changes: Number(r.changes), last_row_id: Number(r.lastInsertRowid) } };
  }
}
