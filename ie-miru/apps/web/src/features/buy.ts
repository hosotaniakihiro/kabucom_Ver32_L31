import { defaultBuyInput, simulateBuy, type BuyInput } from '@ie-miru/domain';
import { formatYen, formatYenRange } from '@ie-miru/ui';
import { clear, h } from '../dom';
import { actionRegistry } from '../views/actions';
import { numField, panelHeader, referencePrice, resultLine, yenField } from './form';

/** 「買う」: 想定購入価格・諸費用・リフォーム予算・総取得費。全項目編集可 */
actionRegistry.set('buy', {
  render({ report, close }) {
    const ref = referencePrice(report);
    const input: BuyInput = defaultBuyInput(ref);
    const result = h('section', { class: 'card', 'data-testid': 'buy-result' });
    const costsBox = h('div', {});
    const el = h(
      'div',
      { class: 'panel', 'data-testid': 'panel-buy' },
      panelHeader('買う（購入の試算）', close),
      ref != null
        ? h('p', { class: 'muted' }, `AI参考査定 ${formatYenRange(report.valuation.value.estimatedLow, report.valuation.value.estimatedHigh)} の中央値を初期値にしています。`)
        : h('p', { class: 'warn' }, '参考価格がないため、想定購入価格を入力してください。'),
      yenField('想定購入価格', input.purchasePrice, (v) => ((input.purchasePrice = v), update()), { testid: 'buy-price' }),
      yenField('リフォーム予算', input.renovationBudget, (v) => ((input.renovationBudget = v), update()), { testid: 'buy-renovation' }),
      yenField('借入額', input.loanAmount, (v) => ((input.loanAmount = v), update()), { testid: 'buy-loan' }),
      numField('金利', input.interestRatePct, '%', (v) => ((input.interestRatePct = v), update()), { step: 0.05 }),
      numField('返済期間', input.loanYears, '年', (v) => ((input.loanYears = v), update())),
      h('h2', {}, '諸費用（各項目を編集できます）'),
      costsBox,
      result,
    );
    let built = false;
    function update() {
      const r = simulateBuy(input);
      if (!built) {
        // 諸費用は初回だけ入力欄を作る（入力中にフォーカスを失わないように）
        for (const c of r.costs) costsBox.append(yenField(c.label, c.amount, (v) => ((input.overrides[c.key as keyof BuyInput['overrides']] = v), update()), { note: c.note, estimated: c.estimated, testid: `buy-cost-${c.key}` }));
        built = true;
      }
      clear(result);
      result.append(
        resultLine('想定購入価格', r.purchasePrice),
        resultLine('諸費用 合計', r.totalCosts, 'buy-total-costs'),
        resultLine('リフォーム予算', r.renovationBudget),
        h('div', { class: 'total' }, '総取得費 ', h('span', { class: 'big-number', 'data-testid': 'buy-total' }, formatYen(r.totalAcquisition))),
        h('p', {}, `月々の返済 約${formatYen(r.monthlyPayment)}（自己資金 ${formatYen(r.downPaymentNeeded)}）`),
        ...r.notes.map((n) => h('p', { class: 'fineprint' }, n)),
      );
    }
    update();
    return { el };
  },
});
