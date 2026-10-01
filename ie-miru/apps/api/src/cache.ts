/** isolate 内の小さな TTL 付き LRU。Workers では isolate ごとに独立（永続化は D1 側）。 */
export class TtlCache<V> {
  private map = new Map<string, { v: V; exp: number }>();
  constructor(private readonly max = 200, private readonly ttlMs = 60 * 60 * 1000, private readonly now = () => Date.now()) {}
  get(k: string): V | undefined {
    const e = this.map.get(k);
    if (!e) return undefined;
    if (e.exp < this.now()) {
      this.map.delete(k);
      return undefined;
    }
    this.map.delete(k);
    this.map.set(k, e);
    return e.v;
  }
  set(k: string, v: V) {
    this.map.delete(k);
    this.map.set(k, { v, exp: this.now() + this.ttlMs });
    while (this.map.size > this.max) this.map.delete(this.map.keys().next().value!);
  }
}
