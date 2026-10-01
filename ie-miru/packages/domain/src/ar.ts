import { cameraPoseFromMatrix, type CameraPose, type Mat3 } from './heading';

/**
 * 端末の AR 能力。iOS では ARKit の静的 API（isSupported / checkAvailability / supportsSceneReconstruction）
 * から埋め、Web では WebXR の有無から埋める。
 */
export interface ArCapabilities {
  worldTracking: boolean;
  /** ARGeoTrackingConfiguration が端末として対応しているか */
  geoTrackingDevice: boolean;
  /** 現在地で Geo Tracking が使えるか（Apple の対応地域）。不明なら null */
  geoTrackingAvailableHere: boolean | null;
  /** LiDAR（sceneReconstruction / sceneDepth）対応 */
  lidar: boolean;
  /** カメラ利用許可 */
  cameraPermission: 'granted' | 'denied' | 'not_determined' | 'unsupported';
}

/**
 * 追跡モード。上ほど高精度。どれも使えなくても `map_only` で手動選択ができる（アプリ全体は止めない）。
 * - geo:            ARGeoTracking（VPS により 数m・数度の精度）
 * - world_heading:  ARWorldTracking + worldAlignment=.gravityAndHeading（コンパス精度）
 * - camera_compass: AR 非対応。カメラ映像 + CoreMotion/DeviceOrientation で方位
 * - compass_only:   カメラも使えない。方位と位置だけで候補提示
 * - map_only:       方位も使えない。地図で手動選択
 */
export type TrackingMode = 'geo' | 'world_heading' | 'camera_compass' | 'compass_only' | 'map_only';

export interface TrackingDecision {
  mode: TrackingMode;
  lidarAssist: boolean;
  notes: string[];
}

export function chooseTrackingMode(cap: ArCapabilities, opts: { hasHeading: boolean; hasLocation: boolean }): TrackingDecision {
  const notes: string[] = [];
  const camera = cap.cameraPermission === 'granted';
  if (!camera) notes.push('カメラが使えないため、方位と地図で建物を選びます。');
  if (!opts.hasLocation) {
    notes.push('現在地が使えないため、地図から建物を選んでください。');
    return { mode: 'map_only', lidarAssist: false, notes };
  }
  if (camera && cap.worldTracking && cap.geoTrackingDevice && cap.geoTrackingAvailableHere === true) {
    return { mode: 'geo', lidarAssist: cap.lidar, notes };
  }
  if (camera && cap.worldTracking) {
    if (cap.geoTrackingDevice && cap.geoTrackingAvailableHere === false) notes.push('この地域は Geo Tracking 対象外のため、コンパス精度で特定します。');
    return { mode: 'world_heading', lidarAssist: cap.lidar, notes };
  }
  if (!opts.hasHeading) {
    notes.push('方位が取得できないため、地図から建物を選んでください。');
    return { mode: 'map_only', lidarAssist: false, notes };
  }
  if (camera) return { mode: 'camera_compass', lidarAssist: false, notes };
  return { mode: 'compass_only', lidarAssist: false, notes };
}

/** モードごとの想定誤差（候補抽出の幅や利用者向け説明に使う） */
export const TRACKING_EXPECTED_ERROR: Record<TrackingMode, { positionM: number; headingDeg: number; label: string }> = {
  geo: { positionM: 5, headingDeg: 5, label: 'AR位置合わせ（高精度）' },
  world_heading: { positionM: 15, headingDeg: 15, label: 'AR＋コンパス' },
  camera_compass: { positionM: 15, headingDeg: 20, label: 'カメラ＋コンパス' },
  compass_only: { positionM: 20, headingDeg: 25, label: 'コンパスのみ' },
  map_only: { positionM: Infinity, headingDeg: Infinity, label: '地図で手動選択' },
};

/**
 * ARKit の camera.transform（列優先 4x4, worldAlignment = .gravityAndHeading）から姿勢を求める。
 * ARKit 世界座標: +X=東, +Y=上, -Z=北。カメラ座標: -Z が光軸, +Y が画面上（横持ち基準）。
 * ARKit のカメラ座標はセンサー基準（landscapeRight）なので縦持ち UI でも光軸は -Z で一致する。
 */
export function cameraPoseFromArkitTransform(columns: number[][], headingAccuracy: number | null = null): CameraPose {
  // ARKit world -> ENU: E = X, N = -Z, U = Y
  const col = (i: number) => columns[i]!;
  const [c0, c1, c2] = [col(0), col(1), col(2)];
  // rotation part R (world <- camera) as rows in ARKit world
  const rw = (r: number, c: number) => [c0, c1, c2][c]![r]!;
  const toEnuRow = (r: 0 | 1 | 2) => {
    // ENU row r expressed from ARKit world rows
    if (r === 0) return [rw(0, 0), rw(0, 1), rw(0, 2)];
    if (r === 1) return [-rw(2, 0), -rw(2, 1), -rw(2, 2)];
    return [rw(1, 0), rw(1, 1), rw(1, 2)];
  };
  // ARKit カメラ座標(+X 右, +Y 上, +Z 手前) は端末座標と同じ規約（縦持ちでは X/Y が入れ替わるがロールにのみ影響）
  const m = [...toEnuRow(0), ...toEnuRow(1), ...toEnuRow(2)] as Mat3;
  return cameraPoseFromMatrix(m, { headingAccuracy, source: 'arkit' });
}
