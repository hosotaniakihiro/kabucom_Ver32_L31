import { mkdirSync, readFileSync, rmSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import type { R2Like, R2ObjectLike } from './types';

/** R2 互換のファイル保存（ローカル開発用）。キーにパス区切り以外の危険文字を許さない */
export class FileR2 implements R2Like {
  constructor(private readonly dir: string) {
    mkdirSync(dir, { recursive: true });
  }
  private path(key: string) {
    if (!/^[A-Za-z0-9/_.-]+$/.test(key) || key.includes('..')) throw new Error('invalid key');
    const p = resolve(this.dir, key);
    if (!p.startsWith(resolve(this.dir))) throw new Error('invalid key');
    return p;
  }
  async put(key: string, value: ArrayBuffer | Uint8Array, opts?: { httpMetadata?: { contentType?: string } }) {
    const p = this.path(key);
    mkdirSync(dirname(p), { recursive: true });
    writeFileSync(p, value instanceof Uint8Array ? value : new Uint8Array(value));
    writeFileSync(`${p}.meta.json`, JSON.stringify(opts?.httpMetadata ?? {}));
  }
  async get(key: string): Promise<R2ObjectLike | null> {
    const p = this.path(key);
    if (!existsSync(p)) return null;
    const buf = readFileSync(p);
    const meta = existsSync(`${p}.meta.json`) ? JSON.parse(readFileSync(`${p}.meta.json`, 'utf8')) : {};
    return {
      body: new Blob([buf]).stream(),
      httpMetadata: meta,
      arrayBuffer: async () => buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) as ArrayBuffer,
    };
  }
  async delete(key: string) {
    rmSync(this.path(key), { force: true });
    rmSync(`${this.path(key)}.meta.json`, { force: true });
  }
}

/** テスト用のメモリ R2 */
export class MemoryR2 implements R2Like {
  readonly objects = new Map<string, { data: Uint8Array; contentType?: string }>();
  async put(key: string, value: ArrayBuffer | Uint8Array, opts?: { httpMetadata?: { contentType?: string } }) {
    this.objects.set(key, { data: value instanceof Uint8Array ? value : new Uint8Array(value), contentType: opts?.httpMetadata?.contentType });
  }
  async get(key: string): Promise<R2ObjectLike | null> {
    const o = this.objects.get(key);
    if (!o) return null;
    return { body: new Blob([o.data as BlobPart]).stream(), httpMetadata: { contentType: o.contentType }, arrayBuffer: async () => o.data.slice().buffer as ArrayBuffer };
  }
  async delete(key: string) {
    this.objects.delete(key);
  }
}

export const joinKey = (...p: string[]) => join(...p).replace(/\\/g, '/');
