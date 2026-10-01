import { LOCATION_DEFAULTS } from '@ie-miru/config';
import type { Building } from './building';
import { angularExtent, bearingDeg, distanceToPolygonM, pointInPolygon, rayPolygonHitDistanceM, signedAngleDiff, type LatLng } from './geo';
import { effectiveHalfFov, type CameraPose } from './heading';

export interface SelectionInput {
  position: LatLng;
  /** 水平精度 [m]。不明なら null */
  accuracyM: number | null;
  pose: CameraPose | null;
  buildings: Building[];
  /** AR raycast / LiDAR で得た注視点までの距離 [m]（あれば候補の絞り込みに使う） */
  measuredDistanceM?: number | null;
  options?: Partial<SelectionOptions>;
}

export interface SelectionOptions {
  radiusM: number;
  halfFovDeg: number;
  minCandidates: number;
  maxCandidates: number;
}

export interface Candidate {
  building: Building;
  /** 現在地から建物外周までの距離 [m] */
  distanceM: number;
  /** 現在地から建物重心への方位 */
  bearingDeg: number;
  /** カメラ方位からのずれ [deg]（視角の端までの距離。視角内なら 0） */
  angleOffsetDeg: number | null;
  inView: boolean;
  /** 中心レイが当たった順位（1=最も手前で見えている）。当たらなければ null */
  hitOrder: number | null;
  hitDistanceM: number | null;
  /** 利用者が中にいる建物 */
  containsUser: boolean;
  score: number;
  reasons: string[];
}

export type SelectionMode =
  /** 最有力候補あり。ただし必ず利用者に「この建物ですか？」と確認する */
  | 'suggest'
  /** 有力候補を絞れない。候補から選んでもらう */
  | 'choose'
  /** 方位/位置が使えない。地図での手動選択を促す（距離順候補は出す） */
  | 'manual';

export interface SelectionResult {
  mode: SelectionMode;
  message: string;
  primaryId: string | null;
  candidates: Candidate[];
  usedHeading: boolean;
  halfFovDeg: number | null;
}

const DEFAULTS: SelectionOptions = {
  radiusM: LOCATION_DEFAULTS.candidateRadiusM,
  halfFovDeg: LOCATION_DEFAULTS.halfFieldOfViewDeg,
  minCandidates: LOCATION_DEFAULTS.minCandidates,
  maxCandidates: LOCATION_DEFAULTS.maxCandidates,
};

export const SELECTION_MESSAGES = {
  suggest: 'この建物ですか？',
  choose: '候補から、見ている建物を選んでください。',
  noHeading: '方位が安定しないため、近い順に表示しています。建物を選んでください。',
  none: '近くに建物データが見つかりません。地図から選んでください。',
  poorLocation: '現在地の精度が低いため、建物を手動選択してください。',
} as const;

/**
 * 建物候補ランキング。
 *   半径内の建物 → カメラ前方±FOV に絞る → 中心レイと建物外周の交差（手前優先・遮蔽考慮）
 *   → 角度ずれ・距離・計測距離でスコア → 上位 2〜5 件
 * 完全自動確定はしない。常に候補を返し、利用者に選ばせる。
 */
export function rankCandidates(input: SelectionInput): SelectionResult {
  const opt = { ...DEFAULTS, ...input.options };
  const { position, pose } = input;
  const acc = input.accuracyM ?? LOCATION_DEFAULTS.accuracyFairM;
  // 位置誤差の分だけ探索半径を広げる（最大 +50m）
  const searchRadius = opt.radiusM + Math.min(50, Math.max(0, acc));

  const near = input.buildings
    .map((b) => ({ b, d: distanceToPolygonM(position, b.footprint) }))
    .filter((x) => x.d <= searchRadius);

  if (near.length === 0) {
    return { mode: 'manual', message: SELECTION_MESSAGES.none, primaryId: null, candidates: [], usedHeading: false, halfFovDeg: null };
  }

  const headingUsable = !!pose && !pose.headingUnreliable;
  const halfFov = headingUsable ? Math.max(opt.halfFovDeg, effectiveHalfFov(pose, opt.halfFovDeg)) : null;

  // 中心レイの交差順（遮蔽: 手前の建物が先に当たる）
  const hits = new Map<string, number>();
  if (headingUsable) {
    near
      .map(({ b }) => ({ id: b.id, t: rayPolygonHitDistanceM(position, pose!.heading, b.footprint) }))
      .filter((h): h is { id: string; t: number } => h.t != null && h.t <= searchRadius)
      .sort((a, b) => a.t - b.t)
      .forEach((h, i) => hits.set(h.id, i + 1));
  }

  const candidates: Candidate[] = near.map(({ b, d }) => {
    const containsUser = pointInPolygon(position, b.footprint);
    const brg = bearingDeg(position, b.centroid);
    const reasons: string[] = [`距離 約${Math.round(d)}m`];
    let angleOffset: number | null = null;
    let inView = false;
    let hitOrder: number | null = null;
    let hitDistance: number | null = null;
    let score: number;
    const distanceScore = 1 - Math.min(1, d / searchRadius);

    if (headingUsable && halfFov != null) {
      const ext = containsUser ? { center: brg, halfWidth: 180 } : angularExtent(position, b.footprint);
      const off = Math.max(0, Math.abs(signedAngleDiff(ext.center, pose!.heading)) - ext.halfWidth);
      angleOffset = Math.round(off * 10) / 10;
      inView = off <= halfFov;
      const h = hits.get(b.id);
      if (h != null) {
        hitOrder = h;
        hitDistance = rayPolygonHitDistanceM(position, pose!.heading, b.footprint);
      }
      const angleScore = inView ? 1 - Math.min(1, off / halfFov) : 0;
      const hitScore = hitOrder === 1 ? 1 : hitOrder != null ? 0.35 : 0;
      score = 0.4 * hitScore + 0.35 * angleScore + 0.25 * distanceScore;

      if (hitOrder === 1) reasons.unshift('カメラの正面にある');
      else if (hitOrder != null) reasons.unshift('正面だが手前の建物に隠れている可能性');
      else if (inView) reasons.unshift(`カメラの視野内（ずれ ${Math.round(off)}°）`);
      else reasons.unshift('カメラの視野外');

      // 計測距離（AR/LiDAR）があれば、交差距離との一致度で補正
      if (input.measuredDistanceM != null && hitDistance != null) {
        const diff = Math.abs(hitDistance - input.measuredDistanceM);
        const bonus = Math.max(-0.15, 0.15 - diff / 40);
        score += bonus;
        if (diff < 6) reasons.push('計測距離と一致');
      }
      if (containsUser) {
        // 中にいる建物を見ている可能性は低い（窓越しの撮影は想定外）
        score *= 0.5;
        reasons.push('現在地がこの建物の中');
      }
    } else {
      score = distanceScore * (containsUser ? 1.05 : 1);
      if (containsUser) reasons.unshift('現在地がこの建物の中');
    }
    return {
      building: b,
      distanceM: Math.round(d * 10) / 10,
      bearingDeg: Math.round(brg * 10) / 10,
      angleOffsetDeg: angleOffset,
      inView,
      hitOrder,
      hitDistanceM: hitDistance == null ? null : Math.round(hitDistance * 10) / 10,
      containsUser,
      score: Math.round(Math.max(0, Math.min(1, score)) * 1000) / 1000,
      reasons,
    };
  });

  candidates.sort((a, b) => b.score - a.score || a.distanceM - b.distanceM || a.building.id.localeCompare(b.building.id));

  // 視野内の候補を優先し、足りなければ近い順に補って最低件数を満たす
  const inView = candidates.filter((c) => !headingUsable || c.inView);
  const rest = candidates.filter((c) => headingUsable && !c.inView).sort((a, b) => a.distanceM - b.distanceM);
  const picked = inView.slice(0, opt.maxCandidates);
  for (const c of rest) {
    if (picked.length >= opt.minCandidates) break;
    picked.push(c);
  }

  if (!headingUsable) {
    return { mode: 'manual', message: SELECTION_MESSAGES.noHeading, primaryId: null, candidates: picked, usedHeading: false, halfFovDeg: null };
  }

  const top = picked[0]!;
  const second = picked[1];
  const margin = top.score - (second?.score ?? 0);
  const strong = top.hitOrder === 1 && top.score >= 0.6 && margin >= 0.15;
  return {
    mode: strong ? 'suggest' : 'choose',
    message: strong ? SELECTION_MESSAGES.suggest : SELECTION_MESSAGES.choose,
    primaryId: strong ? top.building.id : null,
    candidates: picked,
    usedHeading: true,
    halfFovDeg: halfFov,
  };
}
