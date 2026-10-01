import { describe, expect, it } from 'vitest';
import { BRAND, LOCATION_DEFAULTS } from '../src';

describe('brand config', () => {
  it('exposes code name and display name separately', () => {
    expect(BRAND.codeName).toBe('ie-miru');
    expect(BRAND.displayName).toBe('家を見るAI');
  });
  it('keeps candidate count within 2..5', () => {
    expect(LOCATION_DEFAULTS.minCandidates).toBeGreaterThanOrEqual(2);
    expect(LOCATION_DEFAULTS.maxCandidates).toBeLessThanOrEqual(5);
  });
});
