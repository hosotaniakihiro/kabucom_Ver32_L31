/**
 * Cloudflare D1 / R2 の必要最小限のインターフェース。
 * Workers では本物のバインディング、ローカル/テストでは node:sqlite・ファイル実装を差し込む。
 */
export interface D1Result<T = Record<string, unknown>> {
  results: T[];
  success: boolean;
  meta?: { changes?: number; last_row_id?: number };
}

export interface D1PreparedStatementLike {
  bind(...values: unknown[]): D1PreparedStatementLike;
  first<T = Record<string, unknown>>(): Promise<T | null>;
  all<T = Record<string, unknown>>(): Promise<D1Result<T>>;
  run(): Promise<D1Result>;
}

export interface D1Like {
  prepare(sql: string): D1PreparedStatementLike;
  batch(statements: D1PreparedStatementLike[]): Promise<D1Result[]>;
  exec(sql: string): Promise<unknown>;
}

export interface R2ObjectLike {
  body: ReadableStream | null;
  httpMetadata?: { contentType?: string };
  arrayBuffer(): Promise<ArrayBuffer>;
}

export interface R2Like {
  put(key: string, value: ArrayBuffer | Uint8Array, opts?: { httpMetadata?: { contentType?: string } }): Promise<unknown>;
  get(key: string): Promise<R2ObjectLike | null>;
  delete(key: string): Promise<void>;
}
