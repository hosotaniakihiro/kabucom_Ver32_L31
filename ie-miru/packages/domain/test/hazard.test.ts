import { describe, expect, it } from 'vitest';
import { HAZARD_TYPES, hazardStatusExplanation, hazardStatusLabel, severityFromDepth, summarizeHazards, type HazardResult, type HazardStatus } from '../src';

const r = (type: HazardResult['type'], status: HazardStatus, level: string | null = null): HazardResult => ({ type, status, level, severity: null, detail: null, reason: null, sources: [] });

describe('hazard labels never claim safety', () => {
  const statuses: HazardStatus[] = ['in_zone', 'graded', 'no_data', 'not_applicable', 'unavailable'];
  it.each(statuses)('%s label has no 安全', (s) => {
    expect(hazardStatusLabel({ status: s, level: null })).not.toMatch(/安全|区域外/);
  });
  it('distinguishes データなし / 対象外 / 確認できず', () => {
    expect(hazardStatusLabel({ status: 'no_data', level: null })).toBe('データなし');
    expect(hazardStatusLabel({ status: 'not_applicable', level: null })).toBe('対象外');
    expect(hazardStatusLabel({ status: 'unavailable', level: null })).toBe('確認できず');
  });
  it('no_data explanation states it does not mean safe', () => {
    expect(hazardStatusExplanation({ status: 'no_data', reason: null })).toContain('安全を意味するものではありません');
  });
  it('in_zone shows level', () => {
    expect(hazardStatusLabel({ status: 'in_zone', level: '0.5m以上3.0m未満' })).toBe('想定区域内（0.5m以上3.0m未満）');
  });
});

describe('severityFromDepth', () => {
  it.each([
    ['0.5m未満', 1], ['0.5m以上3.0m未満', 2], ['3.0m以上5.0m未満', 3], ['5.0m以上', 4], ['10m以上20m未満', 4], ['不明', null], [null, null],
  ])('%s -> %s', (s, v) => expect(severityFromDepth(s as string | null)).toBe(v));
});

describe('summarizeHazards', () => {
  it('lists in-zone hazards', () => {
    const s = summarizeHazards([r('flood', 'in_zone', '0.5m以上3.0m未満'), r('tsunami', 'no_data'), r('inland_flood', 'unavailable')]);
    expect(s.inZone).toEqual(['flood']);
    expect(s.headline).toBe('想定区域内: 洪水 ／ 確認できず: 内水');
  });
  it('none found is not reported as safe', () => {
    const s = summarizeHazards([r('flood', 'no_data'), r('tsunami', 'not_applicable')]);
    expect(s.headline).toContain('安全を意味しません');
  });
  it('all unavailable', () => {
    expect(summarizeHazards(HAZARD_TYPES.map((t) => r(t, 'unavailable'))).headline).toBe('災害リスクを確認できませんでした');
  });
});
