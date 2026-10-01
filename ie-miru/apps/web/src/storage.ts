/** localStorage はプライベートモード等で例外を投げることがあるため必ず try/catch */
export function load<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

export function save(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* quota / private mode: ignore */
  }
}

export function deviceId(): string {
  let id = load<string | null>('iemiru.deviceId', null);
  if (!id) {
    id = (crypto.randomUUID?.() ?? `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`).replace(/[^A-Za-z0-9-]/g, '');
    save('iemiru.deviceId', id);
  }
  return id;
}
