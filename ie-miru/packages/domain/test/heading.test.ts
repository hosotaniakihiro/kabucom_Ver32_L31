import { describe, expect, it } from 'vitest';
import {
  alphaFromCompassHeading, cameraPoseFromCompassOnly, cameraPoseFromCoreMotionQuaternion, cameraPoseFromDeviceOrientation,
  cameraPoseFromMatrix, circularMean, effectiveHalfFov, headingToYaw, matrixFromDeviceOrientation, normalizeHeading,
  parseCameraPose, smoothHeading,
} from '../src';

describe('heading normalization', () => {
  it.each([[0, 0], [360, 0], [-10, 350], [725, 5]])('%s -> %s', (a, b) => expect(normalizeHeading(a)).toBeCloseTo(b));
  it('yaw is signed', () => {
    expect(headingToYaw(270)).toBe(-90);
    expect(headingToYaw(90)).toBe(90);
  });
});

describe('camera pose from DeviceOrientation', () => {
  it('upright phone with alpha=0 looks north at the horizon', () => {
    const p = cameraPoseFromMatrix(matrixFromDeviceOrientation(0, 90, 0), { source: 'device_orientation' });
    expect(p.heading).toBeCloseTo(0, 5);
    expect(p.pitch).toBeCloseTo(0, 5);
    expect(p.roll).toBeCloseTo(0, 5);
    expect(p.headingUnreliable).toBe(false);
  });
  it('flat phone looks straight down and heading is unreliable', () => {
    const p = cameraPoseFromMatrix(matrixFromDeviceOrientation(0, 0, 0), { source: 'device_orientation' });
    expect(p.pitch).toBeCloseTo(-90, 5);
    expect(p.headingUnreliable).toBe(true);
  });
  it('tilting up raises pitch (camera heading differs from device-top heading)', () => {
    const p = cameraPoseFromMatrix(matrixFromDeviceOrientation(0, 110, 0), { source: 'device_orientation' });
    expect(p.pitch).toBeCloseTo(20, 5);
    expect(p.heading).toBeCloseTo(0, 5);
  });
  it('webkitCompassHeading 90 (east) with upright phone -> camera heading east after declination', () => {
    const p = cameraPoseFromDeviceOrientation({ alpha: 12, beta: 90, gamma: 0, webkitCompassHeading: 90, webkitCompassAccuracy: 10, declinationDeg: 0 })!;
    expect(p.heading).toBeCloseTo(90, 4);
    expect(p.headingAccuracy).toBe(10);
  });
  it('applies magnetic declination (Japan ~ -7.5)', () => {
    expect(alphaFromCompassHeading(0, -7.5)).toBeCloseTo(7.5);
    const p = cameraPoseFromDeviceOrientation({ alpha: 0, beta: 90, gamma: 0, webkitCompassHeading: 0 })!;
    expect(p.heading).toBeCloseTo(352.5, 4);
  });
  it('roll: rotating phone clockwise about the camera axis gives positive roll', () => {
    const base = matrixFromDeviceOrientation(0, 90, 0);
    const t = (-30 * Math.PI) / 180; // device-local rotation about +z (screen normal); negative = clockwise as seen by user
    const rz = [Math.cos(t), -Math.sin(t), 0, Math.sin(t), Math.cos(t), 0, 0, 0, 1];
    const m = Array.from({ length: 9 }, (_, i) => {
      const r = Math.floor(i / 3), c = i % 3;
      return base[r * 3]! * rz[c]! + base[r * 3 + 1]! * rz[3 + c]! + base[r * 3 + 2]! * rz[6 + c]!;
    }) as unknown as Parameters<typeof cameraPoseFromMatrix>[0];
    const p = cameraPoseFromMatrix(m, { source: 'manual' });
    expect(p.roll).toBeCloseTo(30, 5);
    expect(p.heading).toBeCloseTo(0, 5);
    expect(p.pitch).toBeCloseTo(0, 5);
  });
  it('relative alpha without compass is rejected (no north reference)', () => {
    expect(cameraPoseFromDeviceOrientation({ alpha: 10, beta: 90, gamma: 0, absolute: false })).toBeNull();
    expect(cameraPoseFromDeviceOrientation({ alpha: 10, beta: null, gamma: 0, absolute: true })).toBeNull();
  });
  it('absolute alpha (Android-style) is accepted', () => {
    const p = cameraPoseFromDeviceOrientation({ alpha: 270, beta: 90, gamma: 0, absolute: true })!;
    expect(p.heading).toBeCloseTo(90, 4);
  });
});

describe('camera pose from CoreMotion xTrueNorthZVertical', () => {
  it('identity attitude (flat, top to north) looks down', () => {
    const p = cameraPoseFromCoreMotionQuaternion({ x: 0, y: 0, z: 0, w: 1 });
    expect(p.pitch).toBeCloseTo(-90, 5);
  });
  it('rotated +90° about X looks west at horizon', () => {
    const s = Math.SQRT1_2;
    const p = cameraPoseFromCoreMotionQuaternion({ x: s, y: 0, z: 0, w: s }, 5);
    expect(p.pitch).toBeCloseTo(0, 5);
    expect(p.heading).toBeCloseTo(270, 5);
    expect(p.source).toBe('coremotion');
  });
});

describe('heading helpers', () => {
  it('circular mean across north', () => {
    expect(circularMean([350, 10])).toBeCloseTo(0, 5);
    expect(circularMean([])).toBeNull();
    expect(circularMean([0, 180])).toBeNull();
  });
  it('smoothHeading takes the short way around', () => {
    expect(smoothHeading(350, 10, 0.5)).toBeCloseTo(0, 5);
    expect(smoothHeading(null, 370)).toBeCloseTo(10, 5);
  });
  it('effective FOV widens with bad heading accuracy and is capped', () => {
    expect(effectiveHalfFov({ headingAccuracy: 5 })).toBe(20);
    expect(effectiveHalfFov({ headingAccuracy: 30 })).toBe(40);
    expect(effectiveHalfFov({ headingAccuracy: 200 })).toBe(60);
    expect(effectiveHalfFov(null)).toBe(35);
  });
  it('compass-only pose', () => {
    const p = cameraPoseFromCompassOnly(450, 60);
    expect(p.heading).toBe(90);
    expect(p.headingUnreliable).toBe(true);
  });
  it('parseCameraPose rejects missing heading and keeps unknown accuracy null', () => {
    expect(parseCameraPose({ pitch: 1 })).toBeNull();
    const p = parseCameraPose({ heading: 10, source: 'weird' })!;
    expect(p.headingAccuracy).toBeNull();
    expect(p.source).toBe('manual');
  });
});
