import { AR_NOTE_STATUSES, createAnchor, relocalizeNotes, type ArNote, type ArNoteStatus, type CameraPose, type LocationFix } from '@ie-miru/domain';
import { formatHeading } from '@ie-miru/ui';
import { api } from '../api';
import { clear, h, toast } from '../dom';
import { CAMERA_HFOV_DEG, startCamera, watchGeolocation, watchOrientation } from '../sensors';
import { actionRegistry } from '../views/actions';

const STATUS_COLOR: Record<ArNoteStatus, string> = { needs_repair: '#dc2626', check: '#d97706', done: '#16a34a', needs_quote: '#7c3aed' };

/**
 * ARメモ（Web 版の簡易 AR）。照準を修繕箇所に合わせてメモを置き、後日同じ場所を向けると再表示する。
 * 位置合わせは GPS＋方位＋建物外形からの再配置（数m・数度の誤差あり）。高精度は iOS アプリ（ARKit）で行う。
 */
actionRegistry.set('arnote', {
  render({ building, close }) {
    const video = h('video', { class: 'camera', autoplay: true, playsinline: true, muted: true });
    const overlay = h('div', { class: 'ar-overlay', 'data-testid': 'arnote-overlay' });
    const hud = h('div', { class: 'hud', 'data-testid': 'arnote-hud' }, '方位・現在地を取得中…');
    const status = h('select', { 'data-testid': 'arnote-status', 'aria-label': '状態' }, ...Object.entries(AR_NOTE_STATUSES).map(([k, v]) => h('option', { value: k }, v))) as HTMLSelectElement;
    const text = h('input', { type: 'text', placeholder: '例: 外壁のひび', 'data-testid': 'arnote-text' }) as HTMLInputElement;
    const placeBtn = h('button', { class: 'primary', 'data-testid': 'arnote-place' }, 'ここにメモを置く') as HTMLButtonElement;
    const listEl = h('ul', { class: 'mini', 'data-testid': 'arnote-list' });
    const el = h(
      'div',
      { class: 'arnote' },
      h('div', { class: 'arview' }, video, h('div', { class: 'crosshair', 'aria-hidden': 'true' }), overlay, h('header', { class: 'look-top' }, h('button', { class: 'ghost', onclick: close }, '←'), hud)),
      h(
        'section',
        { class: 'panel' },
        h('p', { class: 'muted' }, '画面中央の照準を修繕箇所に合わせてください。精度は GPS・方位に依存します（数m・数度の誤差）。'),
        h('div', { class: 'row' }, status, text, placeBtn),
        h('h2', {}, 'この建物のARメモ'),
        listEl,
      ),
    );

    let fix: LocationFix | null = null;
    let pose: CameraPose | null = null;
    let notes: ArNote[] = [];
    const stops: Array<() => void> = [];
    stops.push(watchGeolocation((s) => s.fix && ((fix = s.fix), render())));
    stops.push(watchOrientation((p) => ((pose = p), render())));
    void startCamera(video).then((r) => (r.ok ? stops.push(r.stop) : el.classList.add('no-camera')));

    async function load() {
      try {
        notes = (await api.call<{ notes: ArNote[] }>(`/v1/ar-notes?buildingId=${encodeURIComponent(building.id)}`)).notes;
      } catch {
        notes = [];
      }
      renderList();
      render();
    }

    function renderList() {
      clear(listEl);
      if (notes.length === 0) listEl.append(h('li', { class: 'muted' }, 'まだメモはありません。'));
      for (const n of notes) {
        const sel = h('select', { 'aria-label': '状態を変更' }, ...Object.entries(AR_NOTE_STATUSES).map(([k, v]) => h('option', { value: k, selected: k === n.status }, v))) as HTMLSelectElement;
        sel.addEventListener('change', async () => {
          await api.call(`/v1/ar-notes/${n.id}`, { method: 'PATCH', body: JSON.stringify({ status: sel.value }) }).catch(() => toast('更新できませんでした'));
          await load();
        });
        listEl.append(h('li', { 'data-testid': 'arnote-item' }, `${n.foundOn} 発見 `, h('strong', {}, n.text || '（メモなし）'), ' ', sel));
      }
    }

    function render() {
      hud.textContent = `${pose ? `方位 ${formatHeading(pose.heading)} 仰角 ${Math.round(pose.pitch)}°` : '方位 取得中'} ・ ${fix ? `精度 ±${Math.round(fix.horizontalAccuracy)}m` : '現在地 取得中'}`;
      clear(overlay);
      if (!fix || !pose) return;
      const placed = relocalizeNotes({ position: { lat: fix.latitude, lng: fix.longitude }, accuracyM: fix.horizontalAccuracy, pose, notes, building, hfovDeg: CAMERA_HFOV_DEG });
      for (const r of placed) {
        if (!r.screen) continue;
        overlay.append(
          h(
            'div',
            { class: 'ar-pin', style: `left:${r.screen.x * 100}%;top:${r.screen.y * 100}%;border-color:${STATUS_COLOR[r.note.status]}`, 'data-testid': 'arnote-pin' },
            h('strong', {}, AR_NOTE_STATUSES[r.note.status]),
            h('span', {}, `${r.note.foundOn.replace(/-/g, '/')} 発見`),
            r.note.text ? h('span', {}, r.note.text) : null,
            h('small', {}, `誤差 ±${Math.round(r.angularErrorDeg)}°`),
          ),
        );
      }
    }

    placeBtn.addEventListener('click', async () => {
      if (!fix || !pose) return toast('現在地・方位を取得中です');
      placeBtn.disabled = true;
      try {
        const anchor = createAnchor({ position: { lat: fix.latitude, lng: fix.longitude }, accuracyM: fix.horizontalAccuracy, altitudeM: fix.altitude, pose, building });
        const today = new Date();
        const foundOn = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
        await api.call('/v1/ar-notes', { method: 'POST', body: JSON.stringify({ buildingId: building.id, status: status.value, text: text.value, foundOn, anchor }) });
        text.value = '';
        toast('メモを置きました');
        await load();
      } catch {
        toast('保存できませんでした');
      } finally {
        placeBtn.disabled = false;
      }
    });

    void load();
    return { el, dispose: () => stops.forEach((s) => s()) };
  },
});
