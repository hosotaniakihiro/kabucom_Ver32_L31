import { describe, expect, it } from 'vitest';
import { cameraPoseFromArkitTransform, chooseTrackingMode, type ArCapabilities } from '../src';

const full: ArCapabilities = { worldTracking: true, geoTrackingDevice: true, geoTrackingAvailableHere: true, lidar: true, cameraPermission: 'granted' };

describe('chooseTrackingMode (AR fallback chain)', () => {
  it('uses geo tracking when available', () => {
    expect(chooseTrackingMode(full, { hasHeading: true, hasLocation: true })).toMatchObject({ mode: 'geo', lidarAssist: true });
  });
  it('falls back to world+heading outside geo-tracking regions', () => {
    const d = chooseTrackingMode({ ...full, geoTrackingAvailableHere: false }, { hasHeading: true, hasLocation: true });
    expect(d.mode).toBe('world_heading');
    expect(d.notes.join()).toContain('Geo Tracking');
  });
  it('unknown geo availability -> world_heading', () => {
    expect(chooseTrackingMode({ ...full, geoTrackingAvailableHere: null }, { hasHeading: true, hasLocation: true }).mode).toBe('world_heading');
  });
  it('non-AR device with camera -> camera_compass', () => {
    const d = chooseTrackingMode({ ...full, worldTracking: false, geoTrackingDevice: false, lidar: false }, { hasHeading: true, hasLocation: true });
    expect(d).toMatchObject({ mode: 'camera_compass', lidarAssist: false });
  });
  it('camera denied -> compass_only, app keeps working', () => {
    const d = chooseTrackingMode({ ...full, cameraPermission: 'denied', worldTracking: false }, { hasHeading: true, hasLocation: true });
    expect(d.mode).toBe('compass_only');
  });
  it('no location -> map_only', () => {
    expect(chooseTrackingMode(full, { hasHeading: true, hasLocation: false }).mode).toBe('map_only');
  });
  it('no heading on non-AR device -> map_only', () => {
    expect(chooseTrackingMode({ ...full, worldTracking: false }, { hasHeading: false, hasLocation: true }).mode).toBe('map_only');
  });
});

describe('cameraPoseFromArkitTransform', () => {
  it('identity transform looks north (ARKit -Z = north)', () => {
    const p = cameraPoseFromArkitTransform([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]);
    expect(p.heading).toBeCloseTo(0, 6);
    expect(p.pitch).toBeCloseTo(0, 6);
    expect(p.source).toBe('arkit');
  });
  it('yawed 90° clockwise (looking east)', () => {
    // rotation about +Y by -90°: camera -Z maps to +X (east)
    const c = Math.cos(-Math.PI / 2), s = Math.sin(-Math.PI / 2);
    const cols = [[c, 0, -s, 0], [0, 1, 0, 0], [s, 0, c, 0], [0, 0, 0, 1]];
    const p = cameraPoseFromArkitTransform(cols);
    expect(p.heading).toBeCloseTo(90, 6);
  });
  it('pitched up 30°', () => {
    const t = Math.PI / 6, c = Math.cos(t), s = Math.sin(t);
    // rotation about +X by +30°: -Z -> (0, sin, -cos)
    const cols = [[1, 0, 0, 0], [0, c, s, 0], [0, -s, c, 0], [0, 0, 0, 1]];
    const p = cameraPoseFromArkitTransform(cols);
    expect(p.pitch).toBeCloseTo(30, 6);
    expect(p.heading).toBeCloseTo(0, 6);
  });
});
