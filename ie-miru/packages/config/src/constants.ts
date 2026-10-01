/** 建物特定・位置情報まわりの既定値。値はすべて調整可能。 */
export const LOCATION_DEFAULTS = {
  /** 候補を集める半径 [m] */
  candidateRadiusM: 100,
  /** カメラ前方の半視野角 [deg]（±20°） */
  halfFieldOfViewDeg: 20,
  /** 表示する候補の最小・最大件数 */
  minCandidates: 2,
  maxCandidates: 5,
  /** 水平精度の判定閾値 [m] */
  accuracyGoodM: 15,
  accuracyFairM: 35,
  /** これ以上古い位置は使わない [ms] */
  maxFixAgeMs: 30_000,
  /** 方位精度がこれより悪い場合は方位を信用しない [deg] */
  headingUnreliableDeg: 45,
  /** 日本の平均的な磁気偏角（西偏, 真北 = 磁北 - 7.5°） */
  defaultMagneticDeclinationDeg: -7.5,
} as const;

export const API_DEFAULTS = {
  /** 外部API 1リクエストのタイムアウト [ms] */
  upstreamTimeoutMs: 8_000,
  /** 再試行回数（5xx / 429 / ネットワーク） */
  upstreamRetries: 2,
  /** 端末IDヘッダ（匿名利用者の識別。個人情報は含めない） */
  deviceHeader: 'X-IeMiru-Device',
} as const;

/** デモ用の既定地点（東京駅丸の内口付近）。位置情報不許可時の地図初期表示にのみ使う。 */
export const DEFAULT_MAP_CENTER = { lat: 35.681236, lng: 139.767125 } as const;
