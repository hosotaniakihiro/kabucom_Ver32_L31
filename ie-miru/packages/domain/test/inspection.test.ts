import { describe, expect, it } from 'vitest';
import { INSPECTION_CATEGORIES, parseMarks, parseMeasurements, validateInspection } from '../src';

describe('inspection', () => {
  it('has the required categories', () => {
    expect(Object.values(INSPECTION_CATEGORIES)).toEqual(expect.arrayContaining(['外壁', '屋根', '雨樋', '窓', '玄関', '塀']));
  });
  it('validates', () => {
    const v = validateInspection({ buildingId: 'b', category: 'roof', marks: [{ x: 0.1, y: 0.2 }], memo: 'x'.repeat(3000) });
    expect(v.ok).toBe(true);
    if (v.ok) {
      expect(v.value.memo.length).toBe(2000);
      expect(v.value.marks[0]!.r).toBe(0.04);
    }
    const bad = validateInspection({ buildingId: '', category: 'kitchen', marks: 'x', memo: null });
    expect(bad.ok).toBe(false);
    if (!bad.ok) expect(bad.errors).toHaveLength(3);
  });
  it('marks are normalized and bounded', () => {
    expect(parseMarks([{ x: 2, y: -1, r: 9 }])).toEqual([{ x: 1, y: 0, r: 0.5, label: null }]);
    expect(parseMarks([{ x: 'a', y: 1 }])).toBeNull();
    expect(parseMarks(new Array(51).fill({ x: 0, y: 0 }))).toBeNull();
    expect(parseMarks(undefined)).toEqual([]);
  });
  it('measurements', () => {
    expect(parseMeasurements([{ kind: 'area', value: 1.23456, method: 'lidar' }, { kind: 'x', value: 1 }, { kind: 'length', value: -1 }])).toEqual([{ kind: 'area', value: 1.235, unit: 'm2', method: 'lidar' }]);
  });
});
