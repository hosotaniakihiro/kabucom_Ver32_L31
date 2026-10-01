import { LOCATION_DEFAULTS } from '@ie-miru/config';
import { isValidLatLng, type LatLng } from './geo';

/**
 * 端末から取得する位置情報。CoreLocation (CLLocation / CLHeading) のフィールドに対応させている。
 * - horizontalAccuracy < 0 は CoreLocation で「無効」を意味する
 * - heading は真北基準。取得できない場合 null（0 にしない）
 */
export interface LocationFix {
  latitude: number;
  longitude: number;
  /** 水平精度 [m]（1σ相当の半径）。負なら無効 */
  horizontalAccuracy: number;
  /** 楕円体高 [m]。不明なら null */
  altitude: number | null;
  /** 端末方位 [deg, 真北, 0..360)。不明なら null */
  heading: number | null;
  /** 方位精度 [deg]。不明なら null */
  headingAccuracy: number | null;
  /** 取得時刻 (epoch ms) */
  timestamp: number;
}

export type LocationPermission = 'granted' | 'denied' | 'restricted' | 'not_determined' | 'unsupported';

export type AccuracyLevel = 'good' | 'fair' | 'poor' | 'invalid';

export type LocationAssessment =
  | { usable: true; level: 'good' | 'fair'; position: LatLng; accuracyM: number; message: string | null; ageMs: number }
  | {
      usable: false;
      reason: 'permission_denied' | 'unsupported' | 'no_fix' | 'invalid' | 'poor_accuracy' | 'stale';
      level: AccuracyLevel;
      position: LatLng | null;
      accuracyM: number | null;
      /** 利用者へ出す文言（手動選択への誘導） */
      message: string;
      ageMs: number | null;
    };

export const LOCATION_MESSAGES = {
  permission_denied: '位置情報が許可されていません。地図から建物を手動で選んでください。',
  unsupported: 'この端末では位置情報を利用できません。地図から建物を手動で選んでください。',
  no_fix: '現在地を取得中です。しばらく待つか、地図から建物を選んでください。',
  invalid: '現在地を取得できませんでした。地図から建物を手動で選んでください。',
  poor_accuracy: '現在地の精度が低いため、建物を手動選択してください。',
  stale: '現在地の情報が古くなっています。少し歩くか、地図から建物を選んでください。',
  fair: '現在地の精度がやや低いため、候補から正しい建物を選んでください。',
} as const;

export function classifyAccuracy(horizontalAccuracy: number | null | undefined): AccuracyLevel {
  if (horizontalAccuracy == null || !Number.isFinite(horizontalAccuracy) || horizontalAccuracy < 0) return 'invalid';
  if (horizontalAccuracy <= LOCATION_DEFAULTS.accuracyGoodM) return 'good';
  if (horizontalAccuracy <= LOCATION_DEFAULTS.accuracyFairM) return 'fair';
  return 'poor';
}

/**
 * 位置情報の使用可否を判定する。例外を投げない（許可拒否・欠損でもクラッシュさせない）。
 */
export function assessLocation(
  fix: LocationFix | null | undefined,
  permission: LocationPermission,
  now: number = Date.now(),
): LocationAssessment {
  if (permission === 'denied' || permission === 'restricted') {
    return { usable: false, reason: 'permission_denied', level: 'invalid', position: null, accuracyM: null, message: LOCATION_MESSAGES.permission_denied, ageMs: null };
  }
  if (permission === 'unsupported') {
    return { usable: false, reason: 'unsupported', level: 'invalid', position: null, accuracyM: null, message: LOCATION_MESSAGES.unsupported, ageMs: null };
  }
  if (!fix) {
    return { usable: false, reason: 'no_fix', level: 'invalid', position: null, accuracyM: null, message: LOCATION_MESSAGES.no_fix, ageMs: null };
  }
  const position = { lat: fix.latitude, lng: fix.longitude };
  if (!isValidLatLng(position)) {
    return { usable: false, reason: 'invalid', level: 'invalid', position: null, accuracyM: null, message: LOCATION_MESSAGES.invalid, ageMs: null };
  }
  const level = classifyAccuracy(fix.horizontalAccuracy);
  const ageMs = Math.max(0, now - fix.timestamp);
  if (level === 'invalid') {
    return { usable: false, reason: 'invalid', level, position, accuracyM: null, message: LOCATION_MESSAGES.invalid, ageMs };
  }
  if (ageMs > LOCATION_DEFAULTS.maxFixAgeMs) {
    return { usable: false, reason: 'stale', level, position, accuracyM: fix.horizontalAccuracy, message: LOCATION_MESSAGES.stale, ageMs };
  }
  if (level === 'poor') {
    return { usable: false, reason: 'poor_accuracy', level, position, accuracyM: fix.horizontalAccuracy, message: LOCATION_MESSAGES.poor_accuracy, ageMs };
  }
  return {
    usable: true,
    level,
    position,
    accuracyM: fix.horizontalAccuracy,
    message: level === 'fair' ? LOCATION_MESSAGES.fair : null,
    ageMs,
  };
}

/**
 * 外部から届いた任意 JSON を LocationFix に正規化する（API入力・Web Geolocation 共通）。
 * 欠損値は null。緯度経度が不正なら null を返す。
 */
export function parseLocationFix(input: unknown): LocationFix | null {
  if (!input || typeof input !== 'object') return null;
  const o = input as Record<string, unknown>;
  const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  const latitude = num(o.latitude ?? o.lat);
  const longitude = num(o.longitude ?? o.lng ?? o.lon);
  if (latitude == null || longitude == null || !isValidLatLng({ lat: latitude, lng: longitude })) return null;
  const heading = num(o.heading);
  return {
    latitude,
    longitude,
    horizontalAccuracy: num(o.horizontalAccuracy ?? o.accuracy) ?? -1,
    altitude: num(o.altitude),
    heading: heading != null && heading >= 0 ? ((heading % 360) + 360) % 360 : null,
    headingAccuracy: num(o.headingAccuracy),
    timestamp: num(o.timestamp) ?? Date.now(),
  };
}

/** 複数の位置サンプルから精度で重み付けした平均位置を作る（短時間の揺れを抑える）。 */
export function fuseFixes(fixes: LocationFix[]): LocationFix | null {
  const valid = fixes.filter((f) => classifyAccuracy(f.horizontalAccuracy) !== 'invalid');
  if (valid.length === 0) return null;
  let wSum = 0;
  let lat = 0;
  let lng = 0;
  for (const f of valid) {
    const w = 1 / Math.max(1, f.horizontalAccuracy) ** 2;
    wSum += w;
    lat += f.latitude * w;
    lng += f.longitude * w;
  }
  const latest = valid.reduce((a, b) => (b.timestamp > a.timestamp ? b : a));
  const fusedAcc = Math.sqrt(1 / wSum);
  return { ...latest, latitude: lat / wSum, longitude: lng / wSum, horizontalAccuracy: Math.max(fusedAcc, 3) };
}
