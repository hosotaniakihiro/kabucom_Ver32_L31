/**
 * 買う / 売る / 貸す の試算。すべての数値は利用者が編集できる前提で、
 * 既定値は「概算（参考情報）」として扱う。税・手数料は簡易計算であり専門家の確認が必要。
 */

export interface CostLine {
  key: string;
  label: string;
  amount: number;
  /** true: 自動計算の概算 / false: 利用者入力 */
  estimated: boolean;
  note?: string;
}

/** 仲介手数料の上限（宅建業法の速算式・税込） */
export function brokerageFee(price: number, taxRate = 0.1): number {
  if (!(price > 0)) return 0;
  let fee: number;
  if (price <= 2_000_000) fee = price * 0.05;
  else if (price <= 4_000_000) fee = price * 0.04 + 20_000;
  else fee = price * 0.03 + 60_000;
  return Math.round(fee * (1 + taxRate));
}

/** 不動産売買契約書の印紙税（軽減措置適用時） */
export function stampDuty(price: number): number {
  const table: Array<[number, number]> = [
    [100_000, 0], [500_000, 200], [1_000_000, 500], [5_000_000, 1_000], [10_000_000, 5_000], [50_000_000, 10_000],
    [100_000_000, 30_000], [500_000_000, 60_000], [1_000_000_000, 160_000], [5_000_000_000, 320_000],
  ];
  for (const [limit, tax] of table) if (price <= limit) return tax;
  return 480_000;
}

/** 元利均等返済の月額 */
export function monthlyPayment(principal: number, annualRatePct: number, years: number): number {
  if (!(principal > 0) || !(years > 0)) return 0;
  const n = Math.round(years * 12);
  const r = annualRatePct / 100 / 12;
  if (r === 0) return Math.round(principal / n);
  return Math.round((principal * r) / (1 - (1 + r) ** -n));
}

// ───────────── 買う ─────────────

export interface BuyInput {
  purchasePrice: number;
  renovationBudget: number;
  loanAmount: number;
  interestRatePct: number;
  loanYears: number;
  /** 個別の上書き（未指定は自動計算） */
  overrides: Partial<Record<'brokerage' | 'stamp' | 'registration' | 'acquisitionTax' | 'loanFee' | 'insurance' | 'other', number>>;
}

export interface BuyResult {
  purchasePrice: number;
  costs: CostLine[];
  totalCosts: number;
  renovationBudget: number;
  totalAcquisition: number;
  monthlyPayment: number;
  downPaymentNeeded: number;
  notes: string[];
}

export function defaultBuyInput(referencePrice: number | null): BuyInput {
  const price = referencePrice != null && referencePrice > 0 ? referencePrice : 0;
  return { purchasePrice: price, renovationBudget: 0, loanAmount: Math.round(price * 0.9), interestRatePct: 1.0, loanYears: 35, overrides: {} };
}

export function simulateBuy(input: BuyInput): BuyResult {
  const p = Math.max(0, input.purchasePrice);
  const o = input.overrides;
  const line = (key: keyof BuyInput['overrides'], label: string, auto: number, note?: string): CostLine =>
    o[key] != null ? { key, label, amount: Math.max(0, o[key]!), estimated: false } : { key, label, amount: Math.round(auto), estimated: true, note };
  const costs: CostLine[] = [
    line('brokerage', '仲介手数料', brokerageFee(p), '上限額（3%+6万円+税）。売主直売なら不要'),
    line('stamp', '印紙税', stampDuty(p), '売買契約書（軽減措置適用時）'),
    line('registration', '登記費用', p > 0 ? p * 0.008 + 100_000 : 0, '登録免許税＋司法書士報酬の概算'),
    line('acquisitionTax', '不動産取得税', p * 0.004, '固定資産税評価額により大きく変わる概算。軽減で0円の場合あり'),
    line('loanFee', 'ローン手数料・保証料', input.loanAmount > 0 ? input.loanAmount * 0.022 : 0, '借入額の2.2%（金融機関により異なる）'),
    line('insurance', '火災・地震保険', p > 0 ? 200_000 : 0, '期間・補償で変動'),
    line('other', 'その他（引越し等）', p > 0 ? 300_000 : 0),
  ];
  const totalCosts = costs.reduce((s, c) => s + c.amount, 0);
  const renovation = Math.max(0, input.renovationBudget);
  const total = p + totalCosts + renovation;
  const loan = Math.min(Math.max(0, input.loanAmount), total);
  return {
    purchasePrice: p,
    costs,
    totalCosts,
    renovationBudget: renovation,
    totalAcquisition: total,
    monthlyPayment: monthlyPayment(loan, input.interestRatePct, input.loanYears),
    downPaymentNeeded: Math.max(0, total - loan),
    notes: ['諸費用は概算です。各項目は入力して上書きできます。', '想定購入価格は参考価格を初期値にしています。実際の売出価格・交渉で決まります。'],
  };
}

// ───────────── 売る ─────────────

export interface SellInput {
  salePrice: number;
  mortgagePayoff: number;
  /** 取得費（購入価格＋購入時諸費用）。不明なら null（税の概算をしない） */
  acquisitionCost: number | null;
  /** 所有期間（年）。null なら不明 */
  holdingYears: number | null;
  /** 居住用財産の3,000万円特別控除を適用 */
  ownHomeDeduction: boolean;
  overrides: Partial<Record<'brokerage' | 'stamp' | 'lienRelease' | 'other', number>>;
}

export interface SellResult {
  salePrice: number;
  costs: CostLine[];
  totalCosts: number;
  mortgagePayoff: number;
  /** 譲渡所得税の概算（不明なら null） */
  capitalGainsTax: number | null;
  taxNote: string;
  netProceeds: number;
  notes: string[];
}

export function defaultSellInput(referencePrice: number | null): SellInput {
  return { salePrice: referencePrice != null && referencePrice > 0 ? referencePrice : 0, mortgagePayoff: 0, acquisitionCost: null, holdingYears: null, ownHomeDeduction: true, overrides: {} };
}

export function simulateSell(input: SellInput): SellResult {
  const p = Math.max(0, input.salePrice);
  const o = input.overrides;
  const line = (key: keyof SellInput['overrides'], label: string, auto: number, note?: string): CostLine =>
    o[key] != null ? { key, label, amount: Math.max(0, o[key]!), estimated: false } : { key, label, amount: Math.round(auto), estimated: true, note };
  const costs: CostLine[] = [
    line('brokerage', '仲介手数料', brokerageFee(p), '上限額（3%+6万円+税）'),
    line('stamp', '印紙税', stampDuty(p)),
    line('lienRelease', '抵当権抹消費用', input.mortgagePayoff > 0 ? 30_000 : 0, '登録免許税＋司法書士報酬の概算'),
    line('other', 'その他（測量・引越し等）', p > 0 ? 300_000 : 0),
  ];
  const totalCosts = costs.reduce((s, c) => s + c.amount, 0);
  let tax: number | null = null;
  let taxNote = '取得費・所有期間が未入力のため、譲渡所得税は計算していません。';
  if (input.acquisitionCost != null && input.holdingYears != null) {
    const gain = p - input.acquisitionCost - totalCosts - (input.ownHomeDeduction ? 30_000_000 : 0);
    const rate = input.holdingYears > 5 ? 0.20315 : 0.3963;
    tax = Math.max(0, Math.round(gain * rate));
    taxNote = `譲渡益×${(rate * 100).toFixed(3)}%（${input.holdingYears > 5 ? '長期' : '短期'}譲渡）${input.ownHomeDeduction ? '・3,000万円特別控除後' : ''}の概算。減価償却・特例要件は税理士に確認してください。`;
  }
  const payoff = Math.max(0, input.mortgagePayoff);
  return {
    salePrice: p,
    costs,
    totalCosts,
    mortgagePayoff: payoff,
    capitalGainsTax: tax,
    taxNote,
    netProceeds: p - totalCosts - payoff - (tax ?? 0),
    notes: ['売却価格はAI参考査定の中央値を初期値にしています。実際の成約価格は査定・市場で変わります。'],
  };
}

// ───────────── 貸す ─────────────

export interface RentInput {
  monthlyRent: number;
  occupancyPct: number;
  /** 管理委託料（家賃の%） */
  managementPct: number;
  annualTaxesAndInsurance: number;
  annualRepairReserve: number;
  /** 利回り計算に使う物件価格（参考価格） */
  propertyValue: number;
}

export interface RentResult {
  annualGrossIncome: number;
  annualExpenses: number;
  annualNetIncome: number;
  grossYieldPct: number | null;
  netYieldPct: number | null;
  notes: string[];
}

/** 想定家賃の初期値: 参考価格 × 想定表面利回り ÷ 12（周辺賃料データは未接続のため参考） */
export function defaultRentInput(propertyValue: number | null, floorAreaM2: number | null): RentInput {
  const v = propertyValue ?? 0;
  const byYield = v > 0 ? (v * 0.05) / 12 : 0;
  const byArea = floorAreaM2 != null ? floorAreaM2 * 2_500 : 0;
  const rent = Math.round((byYield > 0 ? byYield : byArea) / 1000) * 1000;
  return { monthlyRent: rent, occupancyPct: 95, managementPct: 5, annualTaxesAndInsurance: Math.round(v * 0.004), annualRepairReserve: Math.round(rent * 12 * 0.05), propertyValue: v };
}

export function simulateRent(i: RentInput): RentResult {
  const gross = Math.max(0, i.monthlyRent) * 12 * (Math.min(100, Math.max(0, i.occupancyPct)) / 100);
  const expenses = gross * (Math.max(0, i.managementPct) / 100) + Math.max(0, i.annualTaxesAndInsurance) + Math.max(0, i.annualRepairReserve);
  const net = gross - expenses;
  return {
    annualGrossIncome: Math.round(gross),
    annualExpenses: Math.round(expenses),
    annualNetIncome: Math.round(net),
    grossYieldPct: i.propertyValue > 0 ? Math.round(((i.monthlyRent * 12) / i.propertyValue) * 1000) / 10 : null,
    netYieldPct: i.propertyValue > 0 ? Math.round((net / i.propertyValue) * 1000) / 10 : null,
    notes: ['想定家賃は周辺賃料データ未接続のため、参考価格×表面利回り5%からの仮置きです。必ず実際の賃料相場で修正してください。'],
  };
}
