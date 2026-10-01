/**
 * PLATEAU（i-UR / CityGML）属性の正規化。
 * 配信形態（MVT / 3D Tiles batch table / GeoJSON / CSV）や年度によりキー名が違うため、
 * 名前空間・記号・大小文字を無視した「正規化キー」で照合する。
 * 欠損・無効値（-9999 など）は null。0 にしない。
 */

export const PLATEAU_USAGE: Record<string, string> = {
  '401': '業務施設', '402': '商業施設', '403': '宿泊施設', '404': '商業系複合施設',
  '411': '住宅', '412': '共同住宅', '413': '店舗等併用住宅', '414': '店舗等併用共同住宅', '415': '作業所併用住宅',
  '421': '官公庁施設', '422': '文教厚生施設', '431': '運輸倉庫施設', '441': '工場',
  '451': '農林漁業用施設', '452': '供給処理施設', '453': '防衛施設', '454': 'その他', '461': '不明',
};

export const PLATEAU_STRUCTURE: Record<string, string> = {
  '601': '木造・土蔵造', '602': '鉄骨鉄筋コンクリート造', '603': '鉄筋コンクリート造', '604': '鉄骨造',
  '605': '軽量鉄骨造', '606': 'レンガ造・コンクリートブロック造・石造', '611': '不明', '612': '非木造',
};

const normKey = (k: string) => k.replace(/^[a-z]+:/i, '').replace(/[^a-z0-9぀-ヿ一-鿿]/gi, '').toLowerCase();

const KEYS = {
  id: ['gmlid', 'buildingid', 'id', '建物id'],
  height: ['measuredheight', 'height', '計測高さ', '高さ'],
  floorsAbove: ['storeysaboveground', '地上階数', 'floors', 'buildinglevels', 'levels'],
  floorsBelow: ['storeysbelowground', '地下階数', 'buildinglevelsunderground'],
  usage: ['usage', '用途', 'buildingusage', 'detailedusage'],
  structure: ['buildingstructuretype', '構造種別', 'structure', 'buildingstructure'],
  year: ['yearofconstruction', '建築年', 'constructionyear', 'startdate', 'builtyear'],
  name: ['name', '名称', 'buildingname'],
  lod: ['lod', 'maxlod'],
};

function pick(props: Record<string, unknown>, keys: string[]): unknown {
  const map = new Map<string, unknown>();
  for (const [k, v] of Object.entries(props)) map.set(normKey(k), v);
  for (const k of keys) {
    const v = map.get(k);
    if (v !== undefined && v !== null && v !== '') return v;
  }
  return undefined;
}

export function toNumberOrNull(v: unknown, { min = -Infinity, max = Infinity, invalid = [-9999, 9999] }: { min?: number; max?: number; invalid?: number[] } = {}): number | null {
  if (v == null || v === '') return null;
  const n = typeof v === 'number' ? v : Number(String(v).replace(/[^0-9.\-]/g, ''));
  if (!Number.isFinite(n) || invalid.includes(n) || n < min || n > max) return null;
  return n;
}

export interface NormalizedPlateauAttributes {
  id: string | null;
  heightM: number | null;
  floorsAbove: number | null;
  floorsBelow: number | null;
  usage: string | null;
  usageCode: string | null;
  structure: string | null;
  structureCode: string | null;
  builtYear: number | null;
  name: string | null;
  lod: number | null;
}

function codeOrLabel(v: unknown, table: Record<string, string>): { code: string | null; label: string | null } {
  if (v == null || v === '') return { code: null, label: null };
  const s = String(v).trim();
  if (table[s]) return { code: s, label: table[s] };
  const m = /^(\d{3})\b/.exec(s);
  if (m && table[m[1]!]) return { code: m[1]!, label: table[m[1]!]! };
  return { code: null, label: s };
}

export function normalizePlateauAttributes(props: Record<string, unknown>): NormalizedPlateauAttributes {
  const usage = codeOrLabel(pick(props, KEYS.usage), PLATEAU_USAGE);
  const structure = codeOrLabel(pick(props, KEYS.structure), PLATEAU_STRUCTURE);
  const yearRaw = pick(props, KEYS.year);
  const yearMatch = yearRaw == null ? null : /(\d{4})/.exec(String(yearRaw));
  const year = yearMatch ? toNumberOrNull(yearMatch[1], { min: 1800, max: new Date().getFullYear() + 1 }) : null;
  const idRaw = pick(props, KEYS.id);
  const nameRaw = pick(props, KEYS.name);
  const floorsAbove = toNumberOrNull(pick(props, KEYS.floorsAbove), { min: 1, max: 300 });
  const floorsBelow = toNumberOrNull(pick(props, KEYS.floorsBelow), { min: 0, max: 20 });
  return {
    id: idRaw == null ? null : String(idRaw),
    heightM: toNumberOrNull(pick(props, KEYS.height), { min: 0.5, max: 700 }),
    floorsAbove: floorsAbove == null ? null : Math.round(floorsAbove),
    floorsBelow: floorsBelow == null ? null : Math.round(floorsBelow),
    usage: usage.label === '不明' ? null : usage.label,
    usageCode: usage.code,
    structure: structure.label === '不明' ? null : structure.label,
    structureCode: structure.code,
    builtYear: year,
    name: nameRaw == null ? null : String(nameRaw),
    lod: toNumberOrNull(pick(props, KEYS.lod), { min: 0, max: 4 }),
  };
}
