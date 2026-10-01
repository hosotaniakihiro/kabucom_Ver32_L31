import { describe, expect, it } from 'vitest';
import { planRebuild, REBUILD_DISCLAIMER } from '../src';

describe('rebuild plans', () => {
  const base = { landAreaM2: 120, coverageRatioPct: 60, floorAreaRatioPct: 150, useDistrict: '第一種低層住居専用地域' };
  it('2-story within coverage/FAR, references zoning without claiming buildable', () => {
    const p = planRebuild({ preset: 'two_story', ...base });
    expect(p.footprintM2).toBeCloseTo(64.8, 1);
    expect(p.totalFloorM2).toBeCloseTo(129.6, 1);
    expect(p.checks.find((c) => c.key === 'coverage')!.status).toBe('within');
    expect(p.checks.find((c) => c.key === 'far')!.status).toBe('within');
    expect(p.checks.find((c) => c.key === 'height')!.status).toBe('within');
    expect(p.checks.find((c) => c.key === 'zoning')!.status).toBe('unknown');
    expect(p.notes[0]).toBe(REBUILD_DISCLAIMER);
    expect(JSON.stringify(p)).not.toMatch(/建築可能です|建築可能ですが|建てられます/);
  });
  it('3-story exceeds FAR 150% → flagged, not hidden', () => {
    const p = planRebuild({ preset: 'three_story', ...base });
    expect(p.checks.find((c) => c.key === 'far')!.status).toBe('exceeds');
  });
  it('rental combo height vs low-rise limit', () => {
    const p = planRebuild({ preset: 'rental_combo', ...base });
    expect(p.heightM).toBeCloseTo(9.6, 1);
    expect(p.checks.find((c) => c.key === 'height')!.status).toBe('within');
  });
  it('missing zoning data -> unknown checks, uses existing footprint', () => {
    const p = planRebuild({ preset: 'two_story', landAreaM2: null, coverageRatioPct: null, floorAreaRatioPct: null, useDistrict: null, existingFootprintM2: 55 });
    expect(p.footprintM2).toBe(55);
    expect(p.checks.filter((c) => c.status === 'unknown')).toHaveLength(4);
    expect(p.notes.join()).toContain('現在の建物の大きさ');
  });
  it('box dimensions match footprint', () => {
    const p = planRebuild({ preset: 'two_story', ...base });
    expect(Math.abs(p.widthM * p.depthM - p.footprintM2)).toBeLessThan(1);
    expect(p.constructionCostYen).toBeGreaterThan(0);
  });
});
