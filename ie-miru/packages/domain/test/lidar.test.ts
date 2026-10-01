import { describe, expect, it } from 'vitest';
import { describeRepairSize, fuseDistances, observation, planarityRms, polygonArea3, polylineLength, type Vec3 } from '../src';

describe('LiDAR geometry', () => {
  it('area of a 2m x 1.5m wall patch in any orientation', () => {
    const wall: Vec3[] = [[0, 0, 0], [2, 0, 0], [2, 1.5, 0], [0, 1.5, 0]];
    expect(polygonArea3(wall)).toBeCloseTo(3, 6);
    const rotated: Vec3[] = wall.map(([x, y]) => [x * Math.cos(0.7), y, x * Math.sin(0.7)]);
    expect(polygonArea3(rotated)).toBeCloseTo(3, 6);
    expect(planarityRms(rotated)).toBeLessThan(1e-9);
  });
  it('degenerate inputs', () => {
    expect(polygonArea3([[0, 0, 0], [1, 0, 0]])).toBe(0);
    expect(polylineLength([[0, 0, 0]])).toBe(0);
  });
  it('crack length along a polyline', () => {
    expect(polylineLength([[0, 0, 0], [0.3, 0, 0], [0.3, 0.4, 0]])).toBeCloseTo(0.7, 6);
  });
  it('non-planar points are flagged', () => {
    expect(planarityRms([[0, 0, 0], [1, 0, 0], [1, 1, 0.3], [0, 1, 0]])).toBeGreaterThan(0.05);
  });
});

describe('distance fusion', () => {
  it('LiDAR within range dominates GPS', () => {
    const f = fuseDistances([observation(4, 'lidar'), observation(9, 'gps_footprint', 10)])!;
    expect(f.distanceM).toBeCloseTo(4, 1);
    expect(f.source).toBe('lidar');
  });
  it('beyond LiDAR range the error grows', () => {
    expect(observation(20, 'lidar').sigmaM).toBeGreaterThan(observation(4, 'lidar').sigmaM * 5);
  });
  it('AR raycast + GPS', () => {
    const f = fuseDistances([observation(18, 'ar_raycast'), observation(25, 'gps_footprint', 8)])!;
    expect(f.distanceM).toBeGreaterThan(18);
    expect(f.distanceM).toBeLessThan(21);
  });
  it('empty -> null', () => {
    expect(fuseDistances([])).toBeNull();
    expect(fuseDistances([{ distanceM: -1, source: 'lidar', sigmaM: 1 }])).toBeNull();
  });
  it('size description', () => {
    expect(describeRepairSize({ lengthM: 0.35, method: 'lidar' })).toBe('長さ 約35cm（LiDAR計測）');
    expect(describeRepairSize({ areaM2: 3, method: 'ar_plane' })).toBe('面積 約3.00㎡（AR推定）');
    expect(describeRepairSize({ method: 'manual' })).toBe('サイズ未計測');
  });
});
