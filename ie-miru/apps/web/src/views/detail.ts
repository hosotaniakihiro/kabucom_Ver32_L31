import { HAZARD_TYPES, isMock, type Building } from '@ie-miru/domain';
import {
  cell, formatArea, formatMeters, formatPct, formatUnitPrice, formatYear, hazardRow, sourceLine, UI_TEXT, type ValueCell,
} from '@ie-miru/ui';
import { api } from '../api';
import { clear, h } from '../dom';
import { buildingPath, go } from '../router';
import { appState } from '../state';
import { actionRegistry, type ActionContext } from './actions';

export function valueRow(label: string, c: ValueCell, testid?: string): HTMLElement {
  return h(
    'div',
    { class: `vrow ${c.empty ? 'empty' : ''}`, 'data-testid': testid },
    h('span', { class: 'vlabel' }, label),
    h(
      'span',
      { class: 'vvalue' },
      h('span', { class: 'vtext' }, c.text),
      h('span', { class: `badge ${c.badge.kind}` }, c.badge.label),
      c.mock ? h('span', { class: 'badge mock' }, 'モック') : null,
    ),
    c.note ? h('small', { class: 'vnote' }, c.note) : null,
  );
}

function section(title: string, summary: string, body: HTMLElement[], testid: string, open = false): HTMLElement {
  return h('details', { class: 'card', 'data-testid': testid, open }, h('summary', {}, h('strong', {}, title), h('span', { class: 'sum' }, summary)), ...body);
}

/** 建物詳細（下からのシート）。「この家について」→「この家でできること」 */
export function detailView(id: string, action: string | null): { el: HTMLElement; dispose: () => void } {
  const body = h('div', { class: 'sheet-body' }, h('p', { class: 'loading', 'data-testid': 'report-loading' }, '情報を集めています…'));
  const overlay = h('div', { class: 'action-overlay' });
  const el = h(
    'main',
    { class: 'detail' },
    h('div', { class: 'scrim', onclick: () => history.back() }),
    h('section', { class: 'sheet', role: 'dialog', 'aria-label': '建物の詳細' }, h('div', { class: 'grip' }), body),
    overlay,
  );
  let disposed = false;
  let disposeAction: (() => void) | null = null;

  const known = appState.buildings.get(id);
  const hint = known?.centroid ?? appState.lastPosition ?? undefined;
  api
    .report(id, hint ?? undefined)
    .then((r) => {
      if (disposed) return;
      appState.remember([r.building]);
      render(r);
      if (action) openAction(r, action);
    })
    .catch((e) => {
      if (disposed) return;
      clear(body);
      body.append(
        h('p', { class: 'warn', 'data-testid': 'report-error' }, e?.code === 'building_not_found' ? '建物が見つかりませんでした。' : '情報を取得できませんでした（通信エラー）。'),
        h('button', { onclick: () => go(buildingPath(id)) }, '再読み込み'),
      );
    });

  function openAction(report: any, name: string) {
    const def = actionRegistry.get(name);
    if (!def?.render) return;
    disposeAction?.();
    clear(overlay);
    const ctx: ActionContext = { report, building: report.building as Building, close: () => go(buildingPath(id)) };
    const v = def.render(ctx);
    overlay.append(v.el);
    overlay.classList.add('open');
    disposeAction = v.dispose ?? null;
  }

  function render(r: any) {
    clear(body);
    const b: Building = r.building;
    const f = r.facts;
    const title = [f.usage.status === 'available' ? f.usage.value : '建物', f.structure.status === 'available' ? f.structure.value : null].filter(Boolean).join('・');
    const town = r.area.status === 'available' ? r.area.value.townName : null;
    const mockAll = isMock([b.source]) || r.dataMode !== 'live';

    body.append(
      h('header', { class: 'sheet-head' }, h('button', { class: 'ghost', onclick: () => go('/'), 'aria-label': '閉じる' }, '×'), h('div', {}, h('h1', { 'data-testid': 'building-title' }, title), town ? h('p', { class: 'muted' }, `${town}付近`) : null), actionRegistry.get('save')?.inline?.({ report: r, building: b, close: () => {} }) ?? null),
    );
    if (r.offline) body.append(h('p', { class: 'warn', 'data-testid': 'offline-flag' }, `オフライン表示（${new Date(r.offline.cachedAt).toLocaleString('ja-JP')} 取得の情報）`));
    if (mockAll) {
      const msgs = [];
      if (b.source.mode === 'demo') msgs.push('建物はデモデータです（実在しません）');
      if (r.zoning.sources.some((s: any) => s.mode === 'mock')) msgs.push('不動産・ハザード情報はモックデータです（APIキー未設定）');
      if (msgs.length) body.append(h('p', { class: 'demo-flag', 'data-testid': 'mock-flag' }, msgs.join('／')));
    }

    // ── この家について ──
    const aboutEl = h('section', { class: 'about' }, h('h2', {}, UI_TEXT.about));
    // 参考相場
    const marketBody: HTMLElement[] = [];
    let marketSummary = '周辺取引事例';
    const marketHook = actionRegistry.get('__market');
    if (marketHook?.section) {
      const s = marketHook.section(r);
      marketSummary = s.summary;
      marketBody.push(...s.body);
    } else {
      const tx = r.transactions;
      marketBody.push(valueRow('周辺取引事例', cell(tx, (v: any[]) => `${v.length}件`), 'tx-count'));
      marketSummary = tx.status === 'available' ? `周辺取引事例 ${tx.value.length}件` : cell(tx, String).text;
    }
    marketBody.push(h('p', { class: 'fineprint' }, '※この建物そのものの売買価格ではありません。'));
    aboutEl.append(section(UI_TEXT.sections.market, marketSummary, marketBody, 'sec-market'));

    // 土地（地価）
    const lp = r.landPrices;
    const nearest = lp.status === 'available' ? lp.value[0] : null;
    const landBody: HTMLElement[] = [valueRow('最寄りの地価', cell(lp, (v: any[]) => `${formatUnitPrice(v[0].pricePerM2)}（${v[0].kind}・${v[0].year}年・約${formatMeters(v[0].distanceM)}）`), 'land-price')];
    if (lp.status === 'available') {
      const list = h('ul', { class: 'mini' });
      for (const p of lp.value) list.append(h('li', {}, `${p.kind} ${p.year}年: ${formatUnitPrice(p.pricePerM2, false)}${p.yoyChangePct != null ? `（前年比 ${formatPct(p.yoyChangePct, true)}）` : ''} 約${formatMeters(p.distanceM)}${p.address ? `・${p.address}` : ''}`));
      landBody.push(list);
    }
    landBody.push(h('small', { class: 'src' }, sourceLine(lp.sources)));
    aboutEl.append(section(UI_TEXT.sections.land, nearest ? `地価 ${formatUnitPrice(nearest.pricePerM2, false)}` : cell(lp, String).text, landBody, 'sec-land'));

    // 用途地域
    const z = r.zoning;
    const zBody = [
      valueRow('用途地域', cell(z, (v: any) => v.useDistrict), 'zoning-district'),
      valueRow('建ぺい率', cell(z, (v: any) => (v.coverageRatioPct != null ? `${v.coverageRatioPct}%` : 'データなし')), 'zoning-coverage'),
      valueRow('容積率', cell(z, (v: any) => (v.floorAreaRatioPct != null ? `${v.floorAreaRatioPct}%` : 'データなし')), 'zoning-far'),
      z.status === 'available' && z.value.firePrevention ? valueRow('防火規制', cell(z, (v: any) => v.firePrevention)) : null,
      h('small', { class: 'src' }, sourceLine(z.sources)),
    ].filter(Boolean) as HTMLElement[];
    aboutEl.append(section(UI_TEXT.sections.zoning, z.status === 'available' ? `${z.value.useDistrict}` : cell(z, String).text, zBody, 'sec-zoning'));

    // 災害
    const hzBody: HTMLElement[] = [];
    const tbl = h('div', { class: 'hazards' });
    for (const t of HAZARD_TYPES) {
      const hz = r.hazards.find((x: any) => x.type === t);
      if (!hz) continue;
      const row = hazardRow(hz);
      tbl.append(h('div', { class: `hrow ${row.tone}`, 'data-testid': `hazard-${t}` }, h('span', { class: 'hlabel' }, row.label), h('span', { class: 'hstatus' }, row.status), h('small', {}, row.explanation)));
    }
    hzBody.push(tbl, h('small', { class: 'src' }, sourceLine(r.hazards.flatMap((x: any) => x.sources))));
    aboutEl.append(section(UI_TEXT.sections.hazard, r.hazardSummary.headline, hzBody, 'sec-hazard'));

    // 建物概要
    const now = new Date().getFullYear();
    aboutEl.append(
      section(
        '建物概要',
        [f.floorsAbove.status === 'available' ? `${f.floorsAbove.value}階` : null, f.builtYear.status === 'available' ? `${f.builtYear.value}年築` : null].filter(Boolean).join('・') || '概要',
        [
          valueRow('推定築年', cell(f.builtYear, (v: number) => formatYear(v, now)), 'fact-year'),
          valueRow('階数', cell(f.floorsAbove, (v: number) => `地上${v}階`), 'fact-floors'),
          valueRow('高さ', cell(f.heightM, (v: number) => `${v}m`), 'fact-height'),
          valueRow('用途', cell(f.usage, String)),
          valueRow('構造', cell(f.structure, String)),
          valueRow('建物の投影面積', cell(f.footprintAreaM2, (v: number) => formatArea(v))),
          h('small', { class: 'src' }, sourceLine([b.source])),
        ],
        'sec-building',
      ),
    );
    body.append(aboutEl);

    // ── この家でできること ──
    const acts = h('section', { class: 'actions' }, h('h2', {}, UI_TEXT.actions));
    const grid = h('div', { class: 'action-grid' });
    for (const [key, label] of Object.entries(UI_TEXT.actionLabels)) {
      const def = actionRegistry.get(key);
      grid.append(h('button', { class: 'action', disabled: !def?.render, 'data-testid': `action-${key}`, onclick: () => go(buildingPath(id, key)) }, label));
    }
    acts.append(grid);
    const extra = actionRegistry.get('__extra');
    if (extra?.section) acts.append(...extra.section(r).body);
    body.append(acts);

    body.append(h('footer', { class: 'disclaimers' }, ...r.disclaimers.map((d: string) => h('p', { class: 'fineprint' }, d)), h('p', { class: 'fineprint' }, UI_TEXT.disclaimer)));
  }

  return {
    el,
    dispose: () => {
      disposed = true;
      disposeAction?.();
    },
  };
}
