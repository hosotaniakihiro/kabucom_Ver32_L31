import { defaultRentInput, simulateRent, type RentInput } from '@ie-miru/domain';
import { formatYen, formatPct } from '@ie-miru/ui';
import { clear, h } from '../dom';
import { actionRegistry } from '../views/actions';
import { numField, panelHeader, referencePrice, yenField } from './form';

/** 「貸す」: 想定家賃・空室率・経費から利回りを試算（周辺賃料データは未接続のため参考） */
actionRegistry.set('rent', {
  render({ report, close }) {
    const floor = report.subject?.status === 'available' ? report.subject.value.floorAreaM2 : null;
    const input: RentInput = defaultRentInput(referencePrice(report), floor);
    const result = h('section', { class: 'card', 'data-testid': 'rent-result' });
    const el = h(
      'div',
      { class: 'panel', 'data-testid': 'panel-rent' },
      panelHeader('貸す（賃貸の試算）', close),
      yenField('想定家賃（月額）', input.monthlyRent, (v) => ((input.monthlyRent = v), update()), { testid: 'rent-monthly', estimated: true }),
      numField('入居率', input.occupancyPct, '%', (v) => ((input.occupancyPct = v), update())),
      numField('管理委託料', input.managementPct, '%', (v) => ((input.managementPct = v), update())),
      yenField('固定資産税・保険（年）', input.annualTaxesAndInsurance, (v) => ((input.annualTaxesAndInsurance = v), update()), { estimated: true }),
      yenField('修繕積立（年）', input.annualRepairReserve, (v) => ((input.annualRepairReserve = v), update()), { estimated: true }),
      yenField('物件価格（利回り計算用）', input.propertyValue, (v) => ((input.propertyValue = v), update())),
      result,
    );
    function update() {
      const r = simulateRent(input);
      clear(result);
      result.append(
        h('p', {}, `年間家賃収入 ${formatYen(r.annualGrossIncome)} − 経費 ${formatYen(r.annualExpenses)}`),
        h('div', { class: 'total' }, '年間手取り ', h('span', { class: 'big-number', 'data-testid': 'rent-net' }, formatYen(r.annualNetIncome))),
        h('p', {}, `表面利回り ${r.grossYieldPct == null ? '算出不可' : formatPct(r.grossYieldPct)} / 実質利回り ${r.netYieldPct == null ? '算出不可' : formatPct(r.netYieldPct)}`),
        ...r.notes.map((n) => h('p', { class: 'fineprint' }, n)),
      );
    }
    update();
    return { el };
  },
});
