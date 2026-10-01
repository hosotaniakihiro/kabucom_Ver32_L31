/** 金額・面積などの表示フォーマット。値が無いときは呼び出し側で「データなし」等を出す（0 と書かない）。 */

const nf = new Intl.NumberFormat('ja-JP');

/** 円 → 「4,200万円」「1億2,300万円」 */
export function formatYen(yen: number): string {
  if (!Number.isFinite(yen)) return '-';
  const man = Math.round(yen / 10_000);
  if (Math.abs(man) >= 10_000) {
    const oku = Math.trunc(man / 10_000);
    const rest = Math.abs(man % 10_000);
    return rest === 0 ? `${oku}億円` : `${oku}億${nf.format(rest)}万円`;
  }
  return `${nf.format(man)}万円`;
}

/** レンジ表示 「3,900〜4,400万円」。単位が同じなら前側の単位を省く */
export function formatYenRange(low: number, high: number): string {
  const a = formatYen(low);
  const b = formatYen(high);
  if (a.endsWith('万円') && b.endsWith('万円') && !a.includes('億') && !b.includes('億')) return `${a.replace('万円', '')}〜${b}`;
  return `${a}〜${b}`;
}

/** 円/㎡ → 「51.2万円/㎡（169万円/坪）」 */
export function formatUnitPrice(yenPerM2: number, withTsubo = true): string {
  const m2 = `${(yenPerM2 / 10_000).toFixed(1)}万円/㎡`;
  if (!withTsubo) return m2;
  return `${m2}（${nf.format(Math.round((yenPerM2 * 3.30579) / 10_000))}万円/坪）`;
}

export function formatArea(m2: number): string {
  return `${nf.format(Math.round(m2 * 10) / 10)}㎡`;
}

export function formatMeters(m: number): string {
  return m >= 1000 ? `${(m / 1000).toFixed(1)}km` : `${Math.round(m)}m`;
}

export function formatPct(p: number, signed = false): string {
  const s = `${Math.round(p * 10) / 10}%`;
  return signed && p > 0 ? `+${s}` : s;
}

export function formatYear(y: number, now = new Date().getFullYear()): string {
  return `${y}年（築${Math.max(0, now - y)}年）`;
}

export function formatHeading(deg: number): string {
  const dirs = ['北', '北東', '東', '南東', '南', '南西', '西', '北西'];
  return `${dirs[Math.round((((deg % 360) + 360) % 360) / 45) % 8]} ${Math.round(deg)}°`;
}
