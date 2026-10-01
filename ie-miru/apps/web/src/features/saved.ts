import { SAVED_STATUSES, type SavedBuilding } from '@ie-miru/domain';
import { api } from '../api';
import { h, toast } from '../dom';
import { load, save } from '../storage';
import { actionRegistry } from '../views/actions';

export async function fetchSaved(): Promise<{ list: SavedBuilding[]; offline: boolean }> {
  try {
    const r = await api.call<{ saved: SavedBuilding[] }>('/v1/saved');
    save('iemiru.saved', r.saved);
    return { list: r.saved, offline: false };
  } catch {
    return { list: load<SavedBuilding[]>('iemiru.saved', []), offline: true };
  }
}

/** 詳細ヘッダの「保存」ボタン。ニックネームと状態を付けて保存する */
actionRegistry.set('save', {
  inline({ building, report }) {
    const btn = h('button', { class: 'save-btn', 'data-testid': 'save-button' }, '☆ 保存') as HTMLButtonElement;
    const dialog = h('dialog', { class: 'save-dialog', 'data-testid': 'save-dialog' }) as HTMLDialogElement;
    const nickname = h('input', { type: 'text', placeholder: '例: 駅前の青い家', 'data-testid': 'save-nickname', maxlength: 60 }) as HTMLInputElement;
    const status = h('select', { 'data-testid': 'save-status' }, ...Object.entries(SAVED_STATUSES).map(([k, v]) => h('option', { value: k }, v))) as HTMLSelectElement;
    const confirm = h('button', { class: 'primary', 'data-testid': 'save-confirm' }, '保存する');
    dialog.append(h('h2', {}, 'この家を保存'), h('label', { class: 'field' }, h('span', {}, '呼び名'), nickname), h('label', { class: 'field' }, h('span', {}, '状態'), status), h('div', { class: 'row' }, confirm, h('button', { onclick: () => dialog.close() }, 'キャンセル')));
    void fetchSaved().then(({ list }) => {
      const cur = list.find((s) => s.buildingId === building.id);
      if (cur) {
        btn.textContent = '★ 保存済み';
        nickname.value = cur.nickname;
        status.value = cur.status;
      }
    });
    btn.addEventListener('click', () => (dialog.showModal ? dialog.showModal() : dialog.setAttribute('open', '')));
    confirm.addEventListener('click', async () => {
      try {
        await api.call('/v1/saved', {
          method: 'POST',
          body: JSON.stringify({
            buildingId: building.id,
            nickname: nickname.value,
            status: status.value,
            snapshot: { lat: building.centroid.lat, lng: building.centroid.lng, usage: building.usage, sourceMode: building.source.mode },
          }),
        });
        btn.textContent = '★ 保存済み';
        toast('保存しました');
        void fetchSaved();
      } catch {
        toast('保存できませんでした');
      }
      dialog.close?.();
      dialog.removeAttribute('open');
    });
    void report;
    return h('div', {}, btn, dialog);
  },
});
