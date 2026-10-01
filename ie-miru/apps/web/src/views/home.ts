import { BRAND } from '@ie-miru/config';
import { UI_TEXT } from '@ie-miru/ui';
import { h } from '../dom';
import { go } from '../router';
import { requestOrientationPermission } from '../sensors';

/** トップは「見る」「マップ」「保存した家」の3つだけ */
export function homeView(): HTMLElement {
  return h(
    'main',
    { class: 'home' },
    h('h1', { class: 'brand' }, BRAND.displayName),
    h('p', { class: 'tagline' }, BRAND.tagline),
    h(
      'nav',
      { class: 'home-actions' },
      h(
        'button',
        {
          class: 'big primary',
          'data-testid': 'home-look',
          onclick: async () => {
            // iOS はユーザー操作の中で方位センサー許可を求める必要がある
            sessionStorage.setItem('iemiru.orientationPermission', await requestOrientationPermission());
            go('/look');
          },
        },
        h('span', { class: 'icon', 'aria-hidden': 'true' }, '📷'),
        UI_TEXT.home.look,
      ),
      h('button', { class: 'big', 'data-testid': 'home-map', onclick: () => go('/map') }, h('span', { class: 'icon', 'aria-hidden': 'true' }, '🗺'), UI_TEXT.home.map),
      h('button', { class: 'big', 'data-testid': 'home-saved', onclick: () => go('/saved') }, h('span', { class: 'icon', 'aria-hidden': 'true' }, '🏠'), UI_TEXT.home.saved),
    ),
    h('p', { class: 'fineprint' }, BRAND.disclaimer),
  );
}
