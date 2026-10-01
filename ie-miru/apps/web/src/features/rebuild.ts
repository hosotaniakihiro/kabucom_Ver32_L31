import { planRebuild, REBUILD_PRESETS, signedAngleDiff, type RebuildPreset } from '@ie-miru/domain';
import { formatArea, formatYen } from '@ie-miru/ui';
import { clear, h } from '../dom';
import { startCamera, watchOrientation } from '../sensors';
import { actionRegistry } from '../views/actions';
import { panelHeader } from './form';

const STATUS_LABEL = { within: '範囲内（参考）', exceeds: '超過の可能性', unknown: '要確認' } as const;

/** 「建て替える」: 2階建て / 3階建て / 賃貸併用 の簡易ボリュームをカメラ映像に重ねて表示 */
actionRegistry.set('rebuild', {
  render({ report, close }) {
    const subject = report.subject?.status === 'available' ? report.subject.value : null;
    const z = report.zoning?.status === 'available' ? report.zoning.value : null;
    const video = h('video', { class: 'camera', autoplay: true, playsinline: true, muted: true });
    const canvas = h('canvas', { class: 'massing', 'data-testid': 'rebuild-canvas' }) as HTMLCanvasElement;
    const info = h('section', { class: 'card', 'data-testid': 'rebuild-info' });
    const tabs = h('div', { class: 'row', role: 'tablist' });
    const el = h(
      'div',
      { class: 'rebuild' },
      h('div', { class: 'arview' }, video, canvas, h('header', { class: 'look-top' }, h('button', { class: 'ghost', onclick: close }, '←'), h('div', { class: 'hud' }, '建て替え（参考表示）'))),
      h('section', { class: 'panel' }, tabs, info),
    );
    const stops: Array<() => void> = [];
    void startCamera(video).then((r) => (r.ok ? stops.push(r.stop) : el.classList.add('no-camera')));

    let scene: { build: (p: any) => void; setView: (y: number, p: number) => void; dispose: () => void } | null = null;
    let current: RebuildPreset = 'two_story';
    let baseHeading: number | null = null;
    // Three.js は必要になった時だけ読み込む
    void import('./massing').then((m) => {
      scene = m.createMassingScene(canvas);
      stops.push(() => scene?.dispose());
      select(current);
    });
    stops.push(
      watchOrientation((p) => {
        if (baseHeading == null) baseHeading = p.heading;
        scene?.setView(signedAngleDiff(p.heading, baseHeading), p.pitch);
      }),
    );

    function select(preset: RebuildPreset) {
      current = preset;
      for (const b of tabs.querySelectorAll('button')) b.classList.toggle('primary', b.getAttribute('data-preset') === preset);
      const plan = planRebuild({
        preset,
        landAreaM2: subject?.landAreaM2 ?? null,
        coverageRatioPct: z?.coverageRatioPct ?? null,
        floorAreaRatioPct: z?.floorAreaRatioPct ?? null,
        useDistrict: z?.useDistrict ?? null,
        existingFootprintM2: report.building.footprintAreaM2,
      });
      scene?.build(plan);
      clear(info);
      info.append(
        h('h2', {}, `${plan.label}（${plan.structure}）`),
        h('p', {}, plan.description),
        h('p', {}, `建築面積 約${formatArea(plan.footprintM2)} ・ 延床 約${formatArea(plan.totalFloorM2)} ・ 高さ 約${plan.heightM}m`),
        h('p', {}, `建築費の目安 約${formatYen(plan.constructionCostYen)}`, h('span', { class: 'badge reference', style: 'margin-left:6px' }, '参考情報')),
        h('h3', {}, '参考: 用途地域・建ぺい率・容積率'),
        h('ul', { class: 'mini', 'data-testid': 'rebuild-checks' }, ...plan.checks.map((c) => h('li', {}, `${c.label}: ${STATUS_LABEL[c.status]} — ${c.detail}`))),
        ...plan.notes.map((n) => h('p', { class: 'fineprint', 'data-testid': 'rebuild-disclaimer' }, n)),
      );
    }
    for (const [k, v] of Object.entries(REBUILD_PRESETS)) tabs.append(h('button', { 'data-preset': k, 'data-testid': `rebuild-${k}`, onclick: () => select(k as RebuildPreset) }, v.label));
    select(current);
    return { el, dispose: () => stops.forEach((s) => s()) };
  },
});
