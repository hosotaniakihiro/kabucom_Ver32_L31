import { describeRepairSize, INSPECTION_CATEGORIES, type Inspection } from '@ie-miru/domain';
import { api } from '../api';
import { clear, h, toast } from '../dom';
import { actionRegistry } from '../views/actions';
import { panelHeader } from './form';
import { photoMarkup } from './photoMarkup';
import { deviceId } from '../storage';
import { API_DEFAULTS } from '@ie-miru/config';

async function photoUrl(id: string): Promise<string | null> {
  try {
    const res = await fetch(`/v1/inspections/${encodeURIComponent(id)}/photo`, { headers: { [API_DEFAULTS.deviceHeader]: deviceId() } });
    if (!res.ok) return null;
    return URL.createObjectURL(await res.blob());
  } catch {
    return null;
  }
}

/** 「直す」: 写真を撮る → 問題箇所をタップでマーキング → カテゴリ・メモを付けて保存 */
actionRegistry.set('fix', {
  render({ building, close }) {
    const markup = photoMarkup();
    const category = h('select', { 'data-testid': 'fix-category', 'aria-label': '箇所' }, ...Object.entries(INSPECTION_CATEGORIES).map(([k, v]) => h('option', { value: k }, v))) as HTMLSelectElement;
    const memo = h('textarea', { rows: 3, placeholder: '例: 外壁にひび。幅1mmほど', 'data-testid': 'fix-memo' }) as HTMLTextAreaElement;
    const file = h('input', { type: 'file', accept: 'image/*', capture: 'environment', 'data-testid': 'fix-photo' }) as HTMLInputElement;
    const sizeInput = h('input', { type: 'number', inputmode: 'decimal', step: '0.01', min: '0', placeholder: '例 0.35', 'data-testid': 'fix-size', 'aria-label': '長さ（m）' }) as HTMLInputElement;
    const markCount = h('span', { class: 'muted', 'data-testid': 'fix-mark-count' }, 'マーク 0件');
    const listEl = h('div', { class: 'inspections', 'data-testid': 'fix-list' });
    const saveBtn = h('button', { class: 'primary', 'data-testid': 'fix-save' }, '保存する') as HTMLButtonElement;
    markup.onChange(() => (markCount.textContent = `マーク ${markup.marks.length}件`));
    file.addEventListener('change', async () => {
      const f = file.files?.[0];
      if (f) await markup.loadFile(f).catch(() => toast('写真を読み込めませんでした'));
    });
    saveBtn.addEventListener('click', async () => {
      saveBtn.disabled = true;
      try {
        const fd = new FormData();
        fd.set('buildingId', building.id);
        fd.set('category', category.value);
        fd.set('memo', memo.value);
        fd.set('marks', JSON.stringify(markup.marks));
        const len = Number(sizeInput.value);
        // Web では LiDAR を使えないため手入力（iOS アプリでは LiDAR 計測）
        if (sizeInput.value && Number.isFinite(len) && len > 0) fd.set('measurements', JSON.stringify([{ kind: 'length', value: len, method: 'manual' }]));
        const blob = await markup.exportPhoto();
        if (blob) fd.set('photo', new File([blob], 'photo.jpg', { type: 'image/jpeg' }));
        await api.call('/v1/inspections', { method: 'POST', body: fd });
        toast('保存しました');
        memo.value = '';
        await loadList();
      } catch (e: any) {
        toast(e?.code === 'persistence_not_configured' ? '保存先が未設定です' : '保存できませんでした（通信エラー）');
      } finally {
        saveBtn.disabled = false;
      }
    });

    async function loadList() {
      clear(listEl);
      try {
        const r = await api.call<{ inspections: Inspection[] }>(`/v1/inspections?buildingId=${encodeURIComponent(building.id)}`);
        if (r.inspections.length === 0) listEl.append(h('p', { class: 'muted' }, 'まだ記録はありません。'));
        for (const i of r.inspections) {
          const img = h('img', { alt: '', class: 'thumb' }) as HTMLImageElement;
          if (i.photoKey) void photoUrl(i.id).then((u) => u && (img.src = u));
          listEl.append(
            h('div', { class: 'card inspection', 'data-testid': 'fix-item' }, i.photoKey ? img : null, h('div', {}, h('strong', {}, INSPECTION_CATEGORIES[i.category]), ` ${new Date(i.createdAt).toLocaleDateString('ja-JP')}`, h('p', {}, i.memo || '（メモなし）'), h('small', { class: 'muted' }, `マーク${i.marks.length}件`),
              i.measurements.length ? h('small', { class: 'muted' }, ` ・ ${describeRepairSize({ lengthM: i.measurements.find((m) => m.kind === 'length')?.value ?? null, areaM2: i.measurements.find((m) => m.kind === 'area')?.value ?? null, method: i.measurements[0]!.method })}`) : null, h('span', { class: 'badge user', style: 'margin-left:6px' }, 'ユーザー登録'))),
          );
        }
      } catch {
        listEl.append(h('p', { class: 'warn' }, '記録を読み込めませんでした。'));
      }
    }
    void loadList();

    const el = h(
      'div',
      { class: 'panel', 'data-testid': 'panel-fix' },
      panelHeader('直す（修繕記録）', close),
      h('p', { class: 'muted' }, '写真を撮り、気になる箇所をタップして印を付けてください。'),
      h('label', { class: 'field' }, h('span', {}, '箇所'), category),
      h('label', { class: 'field' }, h('span', {}, '写真'), file),
      markup.canvas,
      h('div', { class: 'row' }, markCount, h('button', { onclick: () => markup.undo() }, '1つ戻す')),
      memo,
      h('label', { class: 'field' }, h('span', {}, 'サイズ（長さ・任意）'), h('span', { class: 'row' }, sizeInput, h('span', { class: 'muted' }, 'm')), h('small', {}, 'iPhone アプリでは LiDAR で自動計測できます（対応機種のみ）。')),
      h('div', { class: 'row' }, saveBtn, actionRegistry.has('arnote') ? h('button', { 'data-testid': 'open-arnote', onclick: () => (location.hash = `#/b/${encodeURIComponent(building.id)}/arnote`) }, 'ARメモを置く') : null),
      h('h2', {}, 'これまでの記録'),
      listEl,
    );
    return { el };
  },
});
