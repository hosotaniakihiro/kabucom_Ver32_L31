import { describe, expect, it } from 'vitest';
import { AR_NOTE_STATUSES, createAnchor, destination, relocalizeNotes, validateArNote, type ArNote } from '../src';
import { bldg, ORIGIN } from './helpers';

const house = bldg('demo:H', 0, 25, 14, 10); // 北 20〜30m
const pose = (heading: number, pitch = 10) => ({ heading, pitch, roll: 0, headingAccuracy: 5 });
const note = (anchor: ReturnType<typeof createAnchor>): ArNote => ({ id: 'n1', buildingId: house.id, status: 'needs_repair', text: '外壁のひび', foundOn: '2026-10-02', anchor, createdAt: '', updatedAt: '' });

describe('AR note anchors', () => {
  it('has the four statuses', () => {
    expect(Object.values(AR_NOTE_STATUSES)).toEqual(['修理必要', '確認', '完了', '要見積']);
  });

  it('uses building footprint intersection when no measured distance', () => {
    const a = createAnchor({ position: ORIGIN, accuracyM: 5, pose: pose(0), building: house });
    expect(a.target.distanceSource).toBe('footprint');
    expect(a.target.distanceM).toBeCloseTo(20, 0);
    expect(a.relativeToBuilding!.north).toBeCloseTo(-5, 0); // 南面
    expect(a.relativeToBuilding!.up).toBeCloseTo(1.5 + 20 * Math.tan((10 * Math.PI) / 180), 1);
  });

  it('prefers LiDAR distance (converted to horizontal)', () => {
    const a = createAnchor({ position: ORIGIN, accuracyM: 5, pose: pose(0, 0), building: house, measuredDistanceM: 19.5, measuredBy: 'lidar' });
    expect(a.target).toMatchObject({ distanceM: 19.5, distanceSource: 'lidar' });
  });

  it('no building and no distance -> direction only', () => {
    const a = createAnchor({ position: ORIGIN, accuracyM: 5, pose: pose(90), building: null });
    expect(a.target.distanceM).toBeNull();
    expect(a.relativeToBuilding).toBeNull();
  });

  it('relocalizes at the same place/direction months later', () => {
    const n = note(createAnchor({ position: ORIGIN, accuracyM: 5, pose: pose(0), building: house }));
    const [r] = relocalizeNotes({ position: ORIGIN, accuracyM: 5, pose: pose(0), notes: [n], building: house });
    expect(r!.method).toBe('building_relative');
    expect(Math.abs(r!.offsetDeg)).toBeLessThan(0.5);
    expect(r!.inView).toBe(true);
    expect(r!.screen!.x).toBeCloseTo(0.5, 2);
    expect(r!.screen!.y).toBeCloseTo(0.5, 1);
  });

  it('from a different position the note appears in the correct direction', () => {
    const n = note(createAnchor({ position: ORIGIN, accuracyM: 5, pose: pose(0), building: house }));
    const east10 = destination(ORIGIN, 90, 10);
    const [r] = relocalizeNotes({ position: east10, accuracyM: 5, pose: pose(0), notes: [n], building: house });
    // target is ~20m north of ORIGIN => from 10m east, bearing ≈ atan2(-10, 20) ≈ -26.6°
    expect(r!.offsetDeg).toBeCloseTo(-26.6, 0);
    expect(r!.inView).toBe(false); // 50° hfov → ±25°
    const [r2] = relocalizeNotes({ position: east10, accuracyM: 5, pose: pose(-25), notes: [n], building: house });
    expect(r2!.inView).toBe(true);
  });

  it('larger GPS error increases angular error estimate', () => {
    const n = note(createAnchor({ position: ORIGIN, accuracyM: 5, pose: pose(0), building: house }));
    const a = relocalizeNotes({ position: ORIGIN, accuracyM: 5, pose: pose(0), notes: [n], building: house })[0]!;
    const b = relocalizeNotes({ position: ORIGIN, accuracyM: 30, pose: pose(0), notes: [n], building: house })[0]!;
    expect(b.angularErrorDeg).toBeGreaterThan(a.angularErrorDeg);
  });

  it('falls back to capture pose when the building is not loaded', () => {
    const n = note(createAnchor({ position: ORIGIN, accuracyM: 5, pose: pose(0), building: house }));
    const [r] = relocalizeNotes({ position: ORIGIN, accuracyM: 5, pose: pose(0), notes: [n], building: null });
    expect(r!.method).toBe('capture_pose');
    expect(r!.inView).toBe(true);
  });

  it('validates', () => {
    const a = createAnchor({ position: ORIGIN, accuracyM: 5, pose: pose(0), building: house });
    expect(validateArNote({ buildingId: 'b', status: 'check', text: 't', foundOn: '2026-10-02', anchor: a }).ok).toBe(true);
    const bad = validateArNote({ buildingId: '', status: 'x', foundOn: '10/2', anchor: {} });
    expect(bad.ok).toBe(false);
    if (!bad.ok) expect(bad.errors).toHaveLength(4);
  });
});
