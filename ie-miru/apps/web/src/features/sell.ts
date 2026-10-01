import { defaultSellInput, simulateSell, type SellInput } from '@ie-miru/domain';
import { formatYen, formatYenRange } from '@ie-miru/ui';
import { api } from '../api';
import { clear, h } from '../dom';
import { actionRegistry } from '../views/actions';
import { numField, panelHeader, referencePrice, resultLine, yenField } from './form';

/** 「売る」: AI参考査定・周辺事例・売却諸費用・手取り。不動産会社査定は interface 経由（既定は未接続） */
actionRegistry.set('sell', {
  render({ report, close }) {
    const ref = referencePrice(report);
    const input: SellInput = defaultSellInput(ref);
    const v = report.valuation;
    const c = report.comparables;
    const result = h('section', { class: 'card', 'data-testid': 'sell-result' });
    const costsBox = h('div', {});
    const appraisal = h('p', { class: 'muted', 'data-testid': 'appraisal-status' });
    const el = h(
      'div',
      { class: 'panel', 'data-testid': 'panel-sell' },
      panelHeader('売る（売却の試算）', close),
      h(
        'section',
        { class: 'card' },
        h('p', {}, 'AI参考査定: ', h('strong', { 'data-testid': 'sell-valuation' }, v?.status === 'available' ? formatYenRange(v.value.estimatedLow, v.value.estimatedHigh) : '算出できません')),
        h('p', {}, '周辺取引事例: ', c?.status === 'available' ? `${c.value.count}件（中央 ${c.value.medianPriceYen != null ? formatYen(c.value.medianPriceYen) : '-'}）` : 'データなし'),
        h('p', { class: 'fineprint' }, 'AI参考査定・周辺事例は正式な査定ではありません。'),
      ),
      yenField('想定売却価格', input.salePrice, (x) => ((input.salePrice = x), update()), { testid: 'sell-price' }),
      yenField('ローン残債', input.mortgagePayoff, (x) => ((input.mortgagePayoff = x), update()), { testid: 'sell-payoff' }),
      yenField('取得費（購入価格＋諸費用・任意）', 0, (x) => ((input.acquisitionCost = x > 0 ? x : null), update()), { note: '入力すると譲渡所得税の概算を表示します' }),
      numField('所有期間（任意）', 0, '年', (x) => ((input.holdingYears = x > 0 ? x : null), update())),
      h('label', { class: 'field' }, h('span', {}, '居住用3,000万円特別控除を適用'), (() => {
        const cb = h('input', { type: 'checkbox', checked: true }) as HTMLInputElement;
        cb.addEventListener('change', () => ((input.ownHomeDeduction = cb.checked), update()));
        return cb;
      })()),
      h('h2', {}, '売却諸費用（各項目を編集できます）'),
      costsBox,
      result,
      h(
        'section',
        { class: 'card' },
        h('h2', {}, '不動産会社に査定を依頼'),
        h('p', { class: 'fineprint' }, '特定の会社に依存しない仕組みで接続予定です。連絡先は同意した場合のみ送信します。'),
        h('button', {
          'data-testid': 'appraisal-request',
          onclick: async () => {
            appraisal.textContent = '確認中…';
            try {
              const r = await api.call<any>('/v1/appraisal-requests', { method: 'POST', body: JSON.stringify({ buildingId: report.building.id }) });
              appraisal.textContent = r.responses.map((x: any) => x.message).join(' ');
            } catch {
              appraisal.textContent = '通信できませんでした。';
            }
          },
        }, '査定を依頼する'),
        appraisal,
      ),
    );
    let built = false;
    function update() {
      const r = simulateSell(input);
      if (!built) {
        for (const cl of r.costs) costsBox.append(yenField(cl.label, cl.amount, (x) => ((input.overrides[cl.key as keyof SellInput['overrides']] = x), update()), { note: cl.note, estimated: cl.estimated, testid: `sell-cost-${cl.key}` }));
        built = true;
      }
      clear(result);
      result.append(
        resultLine('想定売却価格', r.salePrice),
        resultLine('売却諸費用', r.totalCosts, 'sell-total-costs'),
        resultLine('ローン残債', r.mortgagePayoff),
        h('div', { class: 'vrow' }, h('span', { class: 'vlabel' }, '譲渡所得税（概算）'), h('span', { class: 'vvalue' }, r.capitalGainsTax == null ? '未計算' : formatYen(r.capitalGainsTax))),
        h('p', { class: 'fineprint' }, r.taxNote),
        h('div', { class: 'total' }, '手取り概算 ', h('span', { class: 'big-number', 'data-testid': 'sell-net' }, formatYen(r.netProceeds))),
        ...r.notes.map((n) => h('p', { class: 'fineprint' }, n)),
      );
    }
    update();
    return { el };
  },
});
