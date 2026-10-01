import { SAVED_STATUSES, type SavedBuilding } from '@ie-miru/domain';
import { api } from '../api';
import { clear, h, toast } from '../dom';
import { buildingPath, go } from '../router';
import { appState } from '../state';
import { fetchSaved } from '../features/saved';

/** 保存した家の一覧。タップで詳細を再表示（通信不可時は前回の一覧・レポートを表示） */
export function savedView(): { el: HTMLElement } {
  const listEl = h('ul', { class: 'saved-list', 'data-testid': 'saved-list' });
  const note = h('p', { class: 'muted' });
  const el = h('main', { class: 'saved' }, h('header', { class: 'bar' }, h('button', { class: 'ghost', onclick: () => go('/') }, '←'), h('h1', {}, '保存した家')), note, listEl);

  async function render() {
    const { list, offline } = await fetchSaved();
    clear(listEl);
    note.textContent = offline ? 'オフラインのため、前回読み込んだ一覧を表示しています。' : list.length === 0 ? 'まだ保存した家はありません。「見る」から建物を選んで保存してください。' : '';
    for (const s of list) listEl.append(item(s));
  }

  function item(s: SavedBuilding) {
    const sel = h('select', { 'aria-label': '状態', 'data-testid': 'saved-status' }, ...Object.entries(SAVED_STATUSES).map(([k, v]) => h('option', { value: k, selected: k === s.status }, v))) as HTMLSelectElement;
    sel.addEventListener('change', async () => {
      await api.call(`/v1/saved/${encodeURIComponent(s.buildingId)}`, { method: 'PATCH', body: JSON.stringify({ status: sel.value }) }).catch(() => toast('更新できませんでした'));
    });
    return h(
      'li',
      { class: 'card' },
      h(
        'button',
        {
          class: 'saved-item ghost',
          'data-testid': 'saved-item',
          onclick: () => {
            if (s.snapshot) appState.lastPosition = { lat: s.snapshot.lat, lng: s.snapshot.lng };
            go(buildingPath(s.buildingId));
          },
        },
        h('strong', {}, s.nickname),
        h('small', { class: 'muted' }, [s.snapshot?.usage, `${new Date(s.updatedAt).toLocaleDateString('ja-JP')} 更新`, s.snapshot?.sourceMode === 'demo' ? 'デモ建物' : null].filter(Boolean).join('・')),
      ),
      h(
        'div',
        { class: 'row' },
        sel,
        h('button', {
          onclick: async () => {
            if (!confirm(`「${s.nickname}」を一覧から削除しますか？`)) return;
            await api.call(`/v1/saved/${encodeURIComponent(s.buildingId)}`, { method: 'DELETE' }).catch(() => toast('削除できませんでした'));
            void render();
          },
        }, '削除'),
      ),
    );
  }

  void render();
  return { el };
}
