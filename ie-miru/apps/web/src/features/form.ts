import { formatYen } from '@ie-miru/ui';
import { h } from '../dom';

/** 金額入力（万円単位で編集、内部は円） */
export function yenField(label: string, yen: number, onChange: (yen: number) => void, opts: { note?: string; testid?: string; estimated?: boolean } = {}) {
  const input = h('input', { type: 'number', inputmode: 'decimal', step: '1', min: '0', value: String(Math.round(yen / 10_000)), 'data-testid': opts.testid, 'aria-label': `${label}（万円）` }) as HTMLInputElement;
  input.addEventListener('input', () => {
    const v = Number(input.value);
    if (Number.isFinite(v)) onChange(Math.round(v * 10_000));
  });
  return h('label', { class: 'field' }, h('span', {}, label, opts.estimated ? h('small', { class: 'badge reference', style: 'margin-left:6px' }, '概算') : null), h('span', { class: 'row' }, input, h('span', { class: 'muted' }, '万円')), opts.note ? h('small', {}, opts.note) : null);
}

export function numField(label: string, value: number, unit: string, onChange: (v: number) => void, opts: { step?: number; testid?: string; note?: string } = {}) {
  const input = h('input', { type: 'number', inputmode: 'decimal', step: String(opts.step ?? 1), value: String(value), 'data-testid': opts.testid, 'aria-label': label }) as HTMLInputElement;
  input.addEventListener('input', () => {
    const v = Number(input.value);
    if (Number.isFinite(v)) onChange(v);
  });
  return h('label', { class: 'field' }, h('span', {}, label), h('span', { class: 'row' }, input, h('span', { class: 'muted' }, unit)), opts.note ? h('small', {}, opts.note) : null);
}

export function panelHeader(title: string, close: () => void) {
  return h('header', { class: 'bar' }, h('button', { class: 'ghost', onclick: close, 'aria-label': '戻る' }, '←'), h('h1', {}, title));
}

export function resultLine(label: string, yen: number, testid?: string, strong = false) {
  return h('div', { class: `vrow ${strong ? 'total' : ''}` }, h('span', { class: 'vlabel' }, label), h('span', { class: 'vvalue', 'data-testid': testid }, formatYen(yen)));
}

/** 参考価格（AI参考査定の中央値）。無ければ null */
export function referencePrice(report: any): number | null {
  return report?.valuation?.status === 'available' ? report.valuation.value.estimatedMid : null;
}
