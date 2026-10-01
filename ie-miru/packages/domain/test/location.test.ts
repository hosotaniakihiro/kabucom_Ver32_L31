import { describe, expect, it } from 'vitest';
import { assessLocation, classifyAccuracy, fuseFixes, parseLocationFix, LOCATION_MESSAGES, type LocationFix } from '../src';

const NOW = 1_800_000_000_000;
const fix = (over: Partial<LocationFix> = {}): LocationFix => ({
  latitude: 35.681236,
  longitude: 139.767125,
  horizontalAccuracy: 8,
  altitude: 40,
  heading: 90,
  headingAccuracy: 10,
  timestamp: NOW - 1000,
  ...over,
});

describe('GPS accuracy classification', () => {
  it.each([
    [5, 'good'],
    [15, 'good'],
    [15.1, 'fair'],
    [35, 'fair'],
    [36, 'poor'],
    [500, 'poor'],
    [-1, 'invalid'],
    [Number.NaN, 'invalid'],
    [null, 'invalid'],
  ])('%s m -> %s', (acc, level) => {
    expect(classifyAccuracy(acc as number | null)).toBe(level);
  });
});

describe('assessLocation', () => {
  it('usable when accurate and fresh', () => {
    const a = assessLocation(fix(), 'granted', NOW);
    expect(a.usable).toBe(true);
    if (a.usable) {
      expect(a.level).toBe('good');
      expect(a.message).toBeNull();
    }
  });
  it('fair accuracy is usable but asks the user to confirm', () => {
    const a = assessLocation(fix({ horizontalAccuracy: 25 }), 'granted', NOW);
    expect(a.usable).toBe(true);
    expect(a.message).toBe(LOCATION_MESSAGES.fair);
  });
  it('poor accuracy falls back to manual selection message', () => {
    const a = assessLocation(fix({ horizontalAccuracy: 80 }), 'granted', NOW);
    expect(a.usable).toBe(false);
    if (!a.usable) {
      expect(a.reason).toBe('poor_accuracy');
      expect(a.message).toBe('現在地の精度が低いため、建物を手動選択してください。');
      expect(a.position).not.toBeNull();
    }
  });
  it('permission denied does not throw and guides to manual map', () => {
    const a = assessLocation(null, 'denied', NOW);
    expect(a.usable).toBe(false);
    if (!a.usable) expect(a.reason).toBe('permission_denied');
  });
  it('restricted and unsupported are handled', () => {
    expect(assessLocation(fix(), 'restricted', NOW).usable).toBe(false);
    const u = assessLocation(undefined, 'unsupported', NOW);
    expect(!u.usable && u.reason).toBe('unsupported');
  });
  it('no fix yet', () => {
    const a = assessLocation(null, 'granted', NOW);
    expect(!a.usable && a.reason).toBe('no_fix');
  });
  it('stale fix is rejected', () => {
    const a = assessLocation(fix({ timestamp: NOW - 120_000 }), 'granted', NOW);
    expect(!a.usable && a.reason).toBe('stale');
  });
  it('negative horizontalAccuracy (CoreLocation invalid) is rejected', () => {
    const a = assessLocation(fix({ horizontalAccuracy: -1 }), 'granted', NOW);
    expect(!a.usable && a.reason).toBe('invalid');
  });
  it('out-of-range coordinates are rejected', () => {
    const a = assessLocation(fix({ latitude: 123 }), 'granted', NOW);
    expect(!a.usable && a.reason).toBe('invalid');
  });
});

describe('parseLocationFix', () => {
  it('accepts web Geolocation-like shape', () => {
    const f = parseLocationFix({ lat: 35, lng: 139, accuracy: 12, timestamp: 5 });
    expect(f).toMatchObject({ latitude: 35, longitude: 139, horizontalAccuracy: 12, altitude: null, heading: null, timestamp: 5 });
  });
  it('keeps missing heading as null, not 0', () => {
    expect(parseLocationFix({ latitude: 35, longitude: 139 })!.heading).toBeNull();
    expect(parseLocationFix({ latitude: 35, longitude: 139, heading: -1 })!.heading).toBeNull();
  });
  it('normalizes heading', () => {
    expect(parseLocationFix({ latitude: 35, longitude: 139, heading: 370 })!.heading).toBe(10);
  });
  it('rejects garbage', () => {
    expect(parseLocationFix(null)).toBeNull();
    expect(parseLocationFix('x')).toBeNull();
    expect(parseLocationFix({ latitude: 'a', longitude: 1 })).toBeNull();
  });
});

describe('fuseFixes', () => {
  it('weights by accuracy', () => {
    const f = fuseFixes([fix({ latitude: 35.0, horizontalAccuracy: 5 }), fix({ latitude: 35.001, horizontalAccuracy: 50 })])!;
    expect(f.latitude).toBeLessThan(35.0001);
  });
  it('returns null when all invalid', () => {
    expect(fuseFixes([fix({ horizontalAccuracy: -1 })])).toBeNull();
  });
});
