import { describe, expect, it } from 'vitest';
import { AppraisalProviderRegistry, NotConnectedAppraisalProvider, type AppraisalProvider } from '../src';

const req = { buildingId: 'demo:1_1', areaLabel: 'モック一丁目', propertyType: '戸建', landAreaM2: 100, floorAreaM2: 90, builtYear: 2000, contact: null };

describe('appraisal providers', () => {
  it('defaults to not connected', async () => {
    const r = await new AppraisalProviderRegistry().requestAll(req);
    expect(r).toEqual([expect.objectContaining({ status: 'not_connected' })]);
  });
  it('fans out to multiple providers without depending on one', async () => {
    const ok: AppraisalProvider = { id: 'a', name: 'A社', capabilities: ['desk'], request: async () => ({ status: 'accepted', providerId: 'a', referenceId: '1', message: 'ok' }) };
    const bad: AppraisalProvider = { id: 'b', name: 'B社', capabilities: ['visit'], request: async () => { throw new Error('down'); } };
    const reg = new AppraisalProviderRegistry().register(ok).register(bad).register(new NotConnectedAppraisalProvider());
    const r = await reg.requestAll(req);
    expect(r.map((x) => x.status)).toEqual(['accepted', 'rejected', 'not_connected']);
    expect(reg.list()).toHaveLength(3);
  });
});
