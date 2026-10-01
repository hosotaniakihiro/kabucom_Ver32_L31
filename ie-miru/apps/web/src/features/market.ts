import { cell, formatArea, formatUnitPrice, formatYen, formatYenRange, sourceLine, UI_TEXT } from '@ie-miru/ui';
import { h } from '../dom';
import { valueRow } from '../views/detail';
import { actionRegistry } from '../views/actions';

/** 「参考相場」セクション: AI参考査定（レンジ）＋周辺取引事例。物件価格と誤認させない表記にする */
actionRegistry.set('__market', {
  section(r: any) {
    const body: HTMLElement[] = [];
    let summary: string = UI_TEXT.comparablesTitle;
    const v = r.valuation;
    if (v) {
      body.push(valueRow(UI_TEXT.valuationTitle, cell(v, (x: any) => `参考価格 ${formatYenRange(x.estimatedLow, x.estimatedHigh)}`), 'valuation'));
      if (v.status === 'available') {
        summary = `参考価格 ${formatYenRange(v.value.estimatedLow, v.value.estimatedHigh)}`;
        body.push(
          h('p', { class: 'muted', 'data-testid': 'valuation-confidence' }, `信頼度: ${v.value.confidenceLabel}（${Math.round(v.value.confidence * 100)}%）`),
          h('ul', { class: 'mini', 'data-testid': 'valuation-reasons' }, ...v.value.reasons.map((x: string) => h('li', {}, x))),
        );
      }
      body.push(h('p', { class: 'fineprint', 'data-testid': 'valuation-disclaimer' }, UI_TEXT.valuationDisclaimer));
    }
    const c = r.comparables;
    body.push(valueRow(`${UI_TEXT.comparablesTitle}`, cell(c, (s: any) => `${s.count}件（期間内 ${s.totalInArea}件）`), 'comparables-count'));
    if (c.status === 'available') {
      const s = c.value;
      if (!v || v.status !== 'available') summary = `${UI_TEXT.marketTitle} ${s.medianPriceYen != null ? formatYen(s.medianPriceYen) : ''}`;
      if (s.p25PriceYen != null && s.p75PriceYen != null) body.push(valueRow('周辺事例の価格帯', cell(c, () => `${formatYenRange(s.p25PriceYen, s.p75PriceYen)}（中央 ${formatYen(s.medianPriceYen)}）`)));
      if (s.medianLandUnitPriceYenPerM2 != null) body.push(valueRow('周辺の土地単価', cell(c, () => formatUnitPrice(s.medianLandUnitPriceYenPerM2))));
      const list = h('ul', { class: 'mini comps', 'data-testid': 'comparables-list' });
      for (const it of s.items.slice(0, 5)) {
        const t = it.transaction;
        list.append(
          h(
            'li',
            {},
            `${t.year}年${t.quarter ? `Q${t.quarter}` : ''} ${t.type} ${formatYen(t.priceYen)}`,
            t.areaM2 != null ? ` 土地${formatArea(t.areaM2)}${t.areaIsLowerBound ? '以上' : ''}` : '',
            t.buildingYear != null ? ` ${t.buildingYear}年築` : '',
            t.district ? ` ${t.district}` : '',
            h('small', { class: 'muted' }, ` 類似度${Math.round(it.similarity * 100)}`),
          ),
        );
      }
      body.push(list);
    } else if (!v) {
      summary = cell(c, String).text;
    }
    body.push(h('small', { class: 'src' }, sourceLine([...(c?.sources ?? []), ...(v?.sources ?? [])])));
    return { summary, body };
  },
});
