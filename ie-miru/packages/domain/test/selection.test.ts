import { describe, expect, it } from 'vitest';
import { cameraPoseFromCompassOnly, rankCandidates, SELECTION_MESSAGES, type CameraPose } from '../src';
import { bldg, ORIGIN } from './helpers';

const pose = (heading: number, acc: number | null = 5): CameraPose => cameraPoseFromCompassOnly(heading, acc);

// 街区: 北に手前(A, 20m)・奥(B, 45m)、東(C, 30m)、南(D, 25m)、北東(E, 40m)、半径外(F, 300m)
const scene = [
  bldg('A', 0, 20, 12, 8),
  bldg('B', 0, 45, 14, 10),
  bldg('C', 30, 0, 10, 10),
  bldg('D', 0, -25, 10, 10),
  bldg('E', 30, 30, 10, 10),
  bldg('F', 0, 300, 10, 10),
];

describe('building selection', () => {
  it('picks the nearest building in front (occlusion aware)', () => {
    const r = rankCandidates({ position: ORIGIN, accuracyM: 5, pose: pose(0), buildings: scene });
    expect(r.usedHeading).toBe(true);
    expect(r.candidates[0]!.building.id).toBe('A');
    expect(r.candidates[0]!.hitOrder).toBe(1);
    const b = r.candidates.find((c) => c.building.id === 'B')!;
    expect(b.hitOrder).toBe(2);
    expect(b.reasons[0]).toContain('隠れている');
    expect(r.mode).toBe('suggest');
    expect(r.primaryId).toBe('A');
    expect(r.message).toBe(SELECTION_MESSAGES.suggest);
  });

  it('turning east selects C', () => {
    const r = rankCandidates({ position: ORIGIN, accuracyM: 5, pose: pose(90), buildings: scene });
    expect(r.candidates[0]!.building.id).toBe('C');
  });

  it('excludes buildings outside radius', () => {
    const r = rankCandidates({ position: ORIGIN, accuracyM: 5, pose: pose(0), buildings: scene });
    expect(r.candidates.map((c) => c.building.id)).not.toContain('F');
  });

  it('returns between 2 and 5 candidates', () => {
    const r = rankCandidates({ position: ORIGIN, accuracyM: 5, pose: pose(180), buildings: scene });
    expect(r.candidates.length).toBeGreaterThanOrEqual(2);
    expect(r.candidates.length).toBeLessThanOrEqual(5);
    expect(r.candidates[0]!.building.id).toBe('D');
  });

  it('caps at 5 in a dense block', () => {
    const dense = Array.from({ length: 12 }, (_, i) => bldg(`N${i}`, (i - 6) * 6, 30 + (i % 3) * 12, 5, 5));
    const r = rankCandidates({ position: ORIGIN, accuracyM: 5, pose: pose(0, 30), buildings: dense });
    expect(r.candidates.length).toBe(5);
  });

  it('pads with nearby buildings when only one is in view', () => {
    const r = rankCandidates({ position: ORIGIN, accuracyM: 5, pose: pose(270), buildings: [bldg('W', -20, 0), bldg('X', 25, 0)] });
    expect(r.candidates.map((c) => c.building.id)).toEqual(['W', 'X']);
    expect(r.candidates[1]!.inView).toBe(false);
  });

  it('nothing in view gives choose mode with nearest buildings', () => {
    const r = rankCandidates({ position: ORIGIN, accuracyM: 5, pose: pose(225), buildings: [bldg('A', 0, 20), bldg('C', 30, 0)] });
    expect(r.mode).toBe('choose');
    expect(r.primaryId).toBeNull();
    expect(r.candidates.length).toBe(2);
  });

  it('does not use GPS alone to decide: unreliable heading -> manual + distance order', () => {
    const p = { ...pose(0), headingUnreliable: true };
    const r = rankCandidates({ position: ORIGIN, accuracyM: 5, pose: p, buildings: scene });
    expect(r.mode).toBe('manual');
    expect(r.primaryId).toBeNull();
    expect(r.usedHeading).toBe(false);
    expect(r.message).toBe(SELECTION_MESSAGES.noHeading);
    expect(r.candidates[0]!.building.id).toBe('A');
  });

  it('null pose -> manual', () => {
    expect(rankCandidates({ position: ORIGIN, accuracyM: 5, pose: null, buildings: scene }).mode).toBe('manual');
  });

  it('no buildings -> manual with message', () => {
    const r = rankCandidates({ position: ORIGIN, accuracyM: 5, pose: pose(0), buildings: [] });
    expect(r).toMatchObject({ mode: 'manual', candidates: [], message: SELECTION_MESSAGES.none });
  });

  it('heading error widens FOV so slightly-off buildings stay candidates', () => {
    // E is at bearing 45 from origin
    const narrow = rankCandidates({ position: ORIGIN, accuracyM: 5, pose: pose(15, 5), buildings: [bldg('E', 30, 30)] });
    const wide = rankCandidates({ position: ORIGIN, accuracyM: 5, pose: pose(15, 40), buildings: [bldg('E', 30, 30)] });
    expect(narrow.candidates[0]!.inView).toBe(false);
    expect(wide.candidates[0]!.inView).toBe(true);
  });

  it('user inside a building is deprioritized when pointing outward', () => {
    const r = rankCandidates({ position: ORIGIN, accuracyM: 5, pose: pose(0), buildings: [bldg('HOME', 0, 0, 20, 20), bldg('A', 0, 25, 10, 8)] });
    expect(r.candidates[0]!.building.id).toBe('A');
    const home = r.candidates.find((c) => c.building.id === 'HOME')!;
    expect(home.containsUser).toBe(true);
  });

  it('measured LiDAR distance disambiguates front vs back building', () => {
    // pointing between A and B lines; measured distance matching B boosts B over occluded penalty only partially
    const base = rankCandidates({ position: ORIGIN, accuracyM: 5, pose: pose(0), buildings: scene });
    const withMeasure = rankCandidates({ position: ORIGIN, accuracyM: 5, pose: pose(0), buildings: scene, measuredDistanceM: 16 });
    const a0 = base.candidates.find((c) => c.building.id === 'A')!.score;
    const a1 = withMeasure.candidates.find((c) => c.building.id === 'A')!.score;
    expect(a1).toBeGreaterThan(a0);
    expect(withMeasure.candidates.find((c) => c.building.id === 'A')!.reasons).toContain('計測距離と一致');
  });

  it('poor accuracy widens search radius', () => {
    const far = bldg('FAR', 0, 130);
    expect(rankCandidates({ position: ORIGIN, accuracyM: 5, pose: pose(0), buildings: [far] }).candidates).toHaveLength(0);
    expect(rankCandidates({ position: ORIGIN, accuracyM: 40, pose: pose(0), buildings: [far] }).candidates).toHaveLength(1);
  });
});
