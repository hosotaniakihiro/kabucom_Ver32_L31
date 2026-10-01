import type { Building } from './building';
import { bearingDeg, destination, distanceM, fromEnu, rayPolygonHitDistanceM, signedAngleDiff, toEnu, type LatLng } from './geo';
import type { CameraPose } from './heading';

/**
 * AR修繕メモ。完全な永続 AR アンカー（ARWorldMap / ARGeoAnchor）は端末・環境依存のため、
 * 以下を併用する「再配置方式」で同じ場所に表示する（docs/IE_MIRU_AR.md）。
 *   1. 建物ID + 建物重心からの相対位置（ENU, m）  … 全端末で利用。精度は GPS/方位次第
 *   2. 撮影時の GPS・カメラ姿勢                       … 1 が作れない場合の代替
 *   3. ARGeoAnchor（Geo Tracking 対応地域）           … iOS のみ。数 m 精度
 *   4. ARWorldMap + ローカル座標（LiDAR 端末で高精度） … iOS のみ。同じ場所で再ローカライズできた場合 cm 精度
 *   5. 写真（修繕記録 inspectionId）                   … 目視で照合するための手掛かり
 */

export const AR_NOTE_STATUSES = {
  needs_repair: '修理必要',
  check: '確認',
  done: '完了',
  needs_quote: '要見積',
} as const;
export type ArNoteStatus = keyof typeof AR_NOTE_STATUSES;

export interface ArAnchor {
  gps: { lat: number; lng: number; accuracyM: number | null; altitudeM: number | null };
  cameraPose: { heading: number; pitch: number; roll: number; headingAccuracy: number | null };
  /** 撮影地点から見たメモ位置の方向・距離 */
  target: { bearingDeg: number; elevationDeg: number; distanceM: number | null; distanceSource: 'lidar' | 'ar_raycast' | 'footprint' | 'none' };
  /** 建物重心からの相対位置 [m]（東, 北, 上）。距離が求まらないときは null */
  relativeToBuilding: { east: number; north: number; up: number } | null;
  /** iOS: ARGeoAnchor の座標 */
  geoAnchor: { lat: number; lng: number; altitudeM: number } | null;
  /** iOS: ARWorldMap の保存キー（R2）と、その座標系での位置 */
  worldMap: { key: string; x: number; y: number; z: number } | null;
  /** 照合用の写真（修繕記録） */
  inspectionId: string | null;
}

export interface ArNote {
  id: string;
  buildingId: string;
  status: ArNoteStatus;
  text: string;
  /** 発見日（YYYY-MM-DD） */
  foundOn: string;
  anchor: ArAnchor;
  createdAt: string;
  updatedAt: string;
}

/** 端末の地上高（目線）[m] */
export const DEVICE_HEIGHT_M = 1.5;

/**
 * 今のカメラが向いている点にアンカーを作る。
 * 距離は 計測値（LiDAR/AR）→ 建物外形との交差 → 不明 の順に使う。
 */
export function createAnchor(input: {
  position: LatLng;
  accuracyM: number | null;
  altitudeM?: number | null;
  pose: Pick<CameraPose, 'heading' | 'pitch' | 'roll' | 'headingAccuracy'>;
  building: Building | null;
  measuredDistanceM?: number | null;
  measuredBy?: 'lidar' | 'ar_raycast';
  geoAnchor?: ArAnchor['geoAnchor'];
  worldMap?: ArAnchor['worldMap'];
  inspectionId?: string | null;
}): ArAnchor {
  const { position, pose, building } = input;
  let dist: number | null = null;
  let source: ArAnchor['target']['distanceSource'] = 'none';
  const cosP = Math.cos((pose.pitch * Math.PI) / 180);
  if (input.measuredDistanceM != null && input.measuredDistanceM > 0) {
    dist = input.measuredDistanceM * cosP; // 水平距離
    source = input.measuredBy ?? 'lidar';
  } else if (building) {
    const hit = rayPolygonHitDistanceM(position, pose.heading, building.footprint);
    if (hit != null && hit > 0) {
      dist = hit;
      source = 'footprint';
    }
  }
  let rel: ArAnchor['relativeToBuilding'] = null;
  if (dist != null && building) {
    const p = destination(position, pose.heading, dist);
    const e = toEnu(building.centroid, p);
    const up = DEVICE_HEIGHT_M + dist * Math.tan((pose.pitch * Math.PI) / 180);
    rel = { east: round2(e.x), north: round2(e.y), up: round2(up) };
  }
  return {
    gps: { lat: position.lat, lng: position.lng, accuracyM: input.accuracyM, altitudeM: input.altitudeM ?? null },
    cameraPose: { heading: round2(pose.heading), pitch: round2(pose.pitch), roll: round2(pose.roll), headingAccuracy: pose.headingAccuracy },
    target: { bearingDeg: round2(pose.heading), elevationDeg: round2(pose.pitch), distanceM: dist == null ? null : round2(dist), distanceSource: source },
    relativeToBuilding: rel,
    geoAnchor: input.geoAnchor ?? null,
    worldMap: input.worldMap ?? null,
    inspectionId: input.inspectionId ?? null,
  };
}

const round2 = (v: number) => Math.round(v * 100) / 100;

export type RelocalizationMethod = 'world_map' | 'geo_anchor' | 'building_relative' | 'capture_pose';

export interface RelocalizedNote {
  note: ArNote;
  method: RelocalizationMethod;
  /** 現在地から見た方位・仰角・水平距離 */
  bearingDeg: number;
  elevationDeg: number;
  distanceM: number | null;
  /** 現在のカメラ中心からのずれ */
  offsetDeg: number;
  /** 画面上の位置（0..1、画面外なら null） */
  screen: { x: number; y: number } | null;
  /** 推定の角度誤差 [deg] */
  angularErrorDeg: number;
  inView: boolean;
}

/**
 * 保存済みメモを今のカメラ姿勢に再配置する（Web / 非 AR 端末・ARWorldMap が無い場合の方式）。
 * iOS で ARWorldMap / ARGeoAnchor が復元できた場合はそちらを優先し、この関数は使わない。
 */
export function relocalizeNotes(input: {
  position: LatLng;
  accuracyM: number | null;
  pose: Pick<CameraPose, 'heading' | 'pitch' | 'headingAccuracy'>;
  notes: ArNote[];
  building: Building | null;
  hfovDeg?: number;
  vfovDeg?: number;
}): RelocalizedNote[] {
  const hfov = input.hfovDeg ?? 50;
  const vfov = input.vfovDeg ?? 65;
  const posErr = input.accuracyM ?? 20;
  const headErr = input.pose.headingAccuracy ?? 20;
  return input.notes.map((note) => {
    const a = note.anchor;
    let target: LatLng;
    let up: number;
    let method: RelocalizationMethod;
    if (a.relativeToBuilding && input.building && input.building.id === note.buildingId) {
      target = fromEnu(input.building.centroid, { x: a.relativeToBuilding.east, y: a.relativeToBuilding.north });
      up = a.relativeToBuilding.up;
      method = 'building_relative';
    } else if (a.target.distanceM != null) {
      target = destination({ lat: a.gps.lat, lng: a.gps.lng }, a.target.bearingDeg, a.target.distanceM);
      up = DEVICE_HEIGHT_M + a.target.distanceM * Math.tan((a.target.elevationDeg * Math.PI) / 180);
      method = 'capture_pose';
    } else {
      // 距離不明: 撮影地点からの方向のみ。撮影地点に近いほど有効
      const fromCapture = distanceM(input.position, { lat: a.gps.lat, lng: a.gps.lng });
      const brg = a.target.bearingDeg;
      const offset = signedAngleDiff(brg, input.pose.heading);
      const err = Math.min(90, headErr + (fromCapture > 1 ? Math.atan2(fromCapture, 10) * (180 / Math.PI) : 0));
      return finish(note, 'capture_pose', brg, a.target.elevationDeg, null, offset, err);
    }
    const d = distanceM(input.position, target);
    const brg = bearingDeg(input.position, target);
    const elev = (Math.atan2(up - DEVICE_HEIGHT_M, Math.max(0.5, d)) * 180) / Math.PI;
    const offset = signedAngleDiff(brg, input.pose.heading);
    const err = Math.min(90, headErr + (Math.atan2(posErr, Math.max(1, d)) * 180) / Math.PI);
    return finish(note, method, brg, elev, d, offset, err);
  });

  function finish(note: ArNote, method: RelocalizationMethod, brg: number, elev: number, d: number | null, offset: number, err: number): RelocalizedNote {
    const dy = elev - input.pose.pitch;
    const inView = Math.abs(offset) <= hfov / 2 && Math.abs(dy) <= vfov / 2;
    return {
      note,
      method,
      bearingDeg: round2(brg),
      elevationDeg: round2(elev),
      distanceM: d == null ? null : round2(d),
      offsetDeg: round2(offset),
      screen: inView ? { x: 0.5 + offset / hfov, y: 0.5 - dy / vfov } : null,
      angularErrorDeg: round2(err),
      inView,
    };
  }
}

export function validateArNote(input: Record<string, unknown>): { ok: true; value: Pick<ArNote, 'buildingId' | 'status' | 'text' | 'foundOn' | 'anchor'> } | { ok: false; errors: string[] } {
  const errors: string[] = [];
  const buildingId = typeof input.buildingId === 'string' && input.buildingId && input.buildingId.length <= 200 ? input.buildingId : null;
  if (!buildingId) errors.push('buildingId が不正です');
  const status = typeof input.status === 'string' && input.status in AR_NOTE_STATUSES ? (input.status as ArNoteStatus) : null;
  if (!status) errors.push('status が不正です');
  const text = typeof input.text === 'string' ? input.text.slice(0, 500) : '';
  const foundOn = typeof input.foundOn === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(input.foundOn) ? input.foundOn : null;
  if (!foundOn) errors.push('foundOn は YYYY-MM-DD');
  const a = input.anchor as ArAnchor | undefined;
  const okAnchor =
    !!a && typeof a === 'object' && !!a.gps && Number.isFinite(a.gps.lat) && Number.isFinite(a.gps.lng) && !!a.cameraPose && Number.isFinite(a.cameraPose.heading) && !!a.target && Number.isFinite(a.target.bearingDeg);
  if (!okAnchor) errors.push('anchor が不正です');
  if (errors.length) return { ok: false, errors };
  return { ok: true, value: { buildingId: buildingId!, status: status!, text, foundOn: foundOn!, anchor: a! } };
}
