import { h } from '../dom';
import { go } from '../router';

export function savedView(): { el: HTMLElement } {
  return { el: h('main', { class: 'saved' }, h('header', { class: 'bar' }, h('button', { class: 'ghost', onclick: () => go('/') }, '←'), h('h1', {}, '保存した家')), h('p', { class: 'muted' }, 'まだ保存した家はありません。')) };
}
