import { assessLocation, signedAngleDiff, type CameraPose, type LocationFix, type LocationPermission } from '@ie-miru/domain';
import { formatHeading, formatMeters, UI_TEXT } from '@ie-miru/ui';
import { api } from '../api';
import { clear, h } from '../dom';
import { buildingPath, go } from '../router';
import { CAMERA_HFOV_DEG, startCamera, watchGeolocation, watchOrientation } from '../sensors';
import { appState } from '../state';

/**
 * 「見る」: 即カメラ。位置＋カメラ方位で建物候補を取り、「この建物ですか？」と2〜5件提示する。
 * カメラ/方位/位置のどれが欠けても画面は止めず、手動選択（地図）へ誘導する。
 */
export function lookView(): { el: HTMLElement; dispose: () => void } {
  const video = h('video', { class: 'camera', autoplay: true, playsinline: true, muted: true, 'data-testid': 'camera' });
  const markers = h('div', { class: 'markers' });
  const hud = h('div', { class: 'hud', 'data-testid': 'hud' });
  const banner = h('div', { class: 'look-banner', 'data-testid': 'look-message' });
  const sheet = h('section', { class: 'candidates', 'data-testid': 'candidates', 'aria-live': 'polite' });
  const manualHeading = h('input', { type: 'range', min: 0, max: 359, value: 0, 'aria-label': '方位を手動で合わせる', 'data-testid': 'manual-heading' }) as HTMLInputElement;
  const manualWrap = h('label', { class: 'manual-heading', hidden: true }, '方位センサーが使えません。向いている方角を合わせてください: ', manualHeading);
  const el = h(
    'main',
    { class: 'look' },
    video,
    markers,
    h('header', { class: 'look-top' }, h('button', { class: 'ghost', onclick: () => go('/'), 'aria-label': '戻る' }, '←'), hud, h('button', { class: 'ghost', onclick: () => go('/map'), 'data-testid': 'look-to-map' }, '地図で選ぶ')),
    banner,
    manualWrap,
    sheet,
  );

  let fix: LocationFix | null = null;
  let permission: LocationPermission = 'not_determined';
  let pose: CameraPose | null = null;
  let lastQuery = { at: 0, heading: -999, lat: 0, lng: 0 };
  let inflight = false;
  let disposed = false;
  let lastSelection: any = null;
  const stops: Array<() => void> = [];

  const orientationPerm = sessionStorage.getItem('iemiru.orientationPermission');
  let gotOrientation = false;
  stops.push(
    watchOrientation((p) => {
      gotOrientation = true;
      manualWrap.hidden = true;
      pose = p;
      renderHud();
      renderMarkers();
      maybeQuery();
    }),
  );
  // 一定時間方位が来なければ手動方位スライダーを出す（PC・非対応端末・許可拒否）
  const t = setTimeout(() => {
    if (!gotOrientation) {
      manualWrap.hidden = false;
      if (orientationPerm === 'denied') banner.textContent = '方位センサーが許可されていません。方角を手動で合わせるか、地図から選んでください。';
      setManualPose();
    }
  }, 1500);
  stops.push(() => clearTimeout(t));
  function setManualPose() {
    pose = { heading: Number(manualHeading.value), yaw: 0, pitch: 0, roll: 0, headingAccuracy: null, headingUnreliable: false, source: 'manual' };
    renderHud();
    renderMarkers();
    maybeQuery(true);
  }
  manualHeading.addEventListener('input', setManualPose);

  stops.push(
    watchGeolocation((s) => {
      permission = s.permission;
      if (s.fix) {
        fix = s.fix;
        appState.lastPosition = { lat: s.fix.latitude, lng: s.fix.longitude };
      }
      renderHud();
      maybeQuery();
    }),
  );

  void startCamera(video).then((r) => {
    if (disposed) {
      if (r.ok) r.stop();
      return;
    }
    if (r.ok) stops.push(r.stop);
    else {
      el.classList.add('no-camera');
      banner.textContent = r.reason === 'denied' ? 'カメラが許可されていません。方位と現在地で候補を表示します。' : 'カメラを起動できません。方位と現在地で候補を表示します。';
    }
  });

  const interval = setInterval(() => maybeQuery(), 2500);
  stops.push(() => clearInterval(interval));

  function renderHud() {
    const a = assessLocation(fix, permission === 'not_determined' && !fix ? 'granted' : permission);
    const parts = [
      pose ? `方位 ${formatHeading(pose.heading)}${pose.source === 'manual' ? '（手動）' : ''}` : '方位 取得中',
      fix ? `精度 ±${Math.round(fix.horizontalAccuracy)}m` : '現在地 取得中',
    ];
    hud.textContent = parts.join(' ・ ');
    if (!a.usable && (a.reason === 'permission_denied' || a.reason === 'poor_accuracy' || a.reason === 'unsupported')) {
      banner.textContent = a.message;
      banner.classList.add('warn');
    } else if (a.usable && a.message) {
      banner.textContent = a.message;
      banner.classList.remove('warn');
    }
  }

  async function maybeQuery(force = false) {
    if (inflight || disposed) return;
    if (!fix && permission !== 'denied' && permission !== 'unsupported') return;
    const now = Date.now();
    const moved = fix ? Math.hypot((fix.latitude - lastQuery.lat) * 111000, (fix.longitude - lastQuery.lng) * 91000) : 0;
    const turned = pose ? Math.abs(signedAngleDiff(pose.heading, lastQuery.heading)) : 0;
    if (!force && now - lastQuery.at < 1200) return;
    if (!force && lastSelection && moved < 4 && turned < 8 && now - lastQuery.at < 10000) return;
    inflight = true;
    lastQuery = { at: now, heading: pose?.heading ?? -999, lat: fix?.latitude ?? 0, lng: fix?.longitude ?? 0 };
    try {
      const res = await api.candidates({ fix, pose, permission: fix ? 'granted' : permission });
      if (disposed) return;
      if (res.selection) appState.remember(res.selection.candidates.map((c: any) => c.building));
      lastSelection = res.selection;
      renderCandidates(res);
      renderMarkers();
    } catch {
      renderError();
    } finally {
      inflight = false;
    }
  }

  function renderError() {
    clear(sheet);
    sheet.append(
      h('p', { class: 'warn' }, '通信できませんでした。電波の良い場所で再度お試しいただくか、保存した家・地図をご利用ください。'),
      h('div', { class: 'row' }, h('button', { onclick: () => maybeQuery(true) }, '再試行'), h('button', { onclick: () => go('/map') }, '地図で選ぶ'), h('button', { onclick: () => go('/saved') }, '保存した家')),
    );
  }

  function renderCandidates(res: any) {
    clear(sheet);
    const sel = res.selection;
    if (!sel) {
      sheet.append(h('p', { class: 'warn' }, res.assessment?.message ?? '現在地を取得できません。'), h('button', { class: 'primary', onclick: () => go('/map') }, '地図から選ぶ'));
      return;
    }
    const demo = res.sources?.some((s: any) => s.mode === 'demo');
    sheet.append(h('h2', {}, sel.mode === 'suggest' ? UI_TEXT.confirm : sel.message));
    if (sel.mode !== 'suggest' && sel.message !== UI_TEXT.confirm && res.assessment?.message) sheet.append(h('p', { class: 'muted' }, res.assessment.message));
    if (demo) sheet.append(h('p', { class: 'demo-flag', 'data-testid': 'demo-flag' }, 'デモ建物データ（実在の建物ではありません）'));
    if (sel.candidates.length === 0) {
      sheet.append(h('button', { class: 'primary', onclick: () => go('/map') }, '地図から選ぶ'));
      return;
    }
    const list = h('ol', { class: 'candidate-list' });
    sel.candidates.forEach((c: any, i: number) => {
      const b = c.building;
      const title = [b.usage ?? '建物', b.floorsAbove ? `${b.floorsAbove}階` : null].filter(Boolean).join('・');
      list.append(
        h(
          'li',
          {},
          h(
            'button',
            {
              class: `candidate ${sel.primaryId === b.id ? 'primary' : ''}`,
              'data-testid': `candidate-${i}`,
              'data-building-id': b.id,
              onclick: () => go(buildingPath(b.id)),
            },
            h('span', { class: 'num' }, String(i + 1)),
            h('span', { class: 'ctitle' }, title),
            h('span', { class: 'cmeta' }, [`約${formatMeters(c.distanceM)}`, ...c.reasons.filter((r: string) => !r.startsWith('距離'))].join('・')),
          ),
        ),
      );
    });
    sheet.append(list, h('button', { class: 'link', onclick: () => go('/map') }, 'どれでもない → 地図で選ぶ'));
  }

  function renderMarkers() {
    clear(markers);
    if (!pose || !lastSelection?.usedHeading) return;
    lastSelection.candidates.forEach((c: any, i: number) => {
      const off = signedAngleDiff(c.bearingDeg, pose!.heading);
      if (Math.abs(off) > CAMERA_HFOV_DEG / 2) return;
      const x = 50 + (off / (CAMERA_HFOV_DEG / 2)) * 50;
      markers.append(h('button', { class: `marker ${lastSelection.primaryId === c.building.id ? 'primary' : ''}`, style: `left:${x}%`, onclick: () => go(buildingPath(c.building.id)), 'aria-label': `候補${i + 1}` }, String(i + 1)));
    });
  }

  renderHud();
  return {
    el,
    dispose: () => {
      disposed = true;
      stops.forEach((s) => s());
    },
  };
}
