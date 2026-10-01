import './styles.css';
import { BRAND } from '@ie-miru/config';
import { clear } from './dom';
import { parseRoute } from './router';
import { homeView } from './views/home';
import { lookView } from './views/look';
import { mapView } from './views/map';
import { detailView } from './views/detail';
import { savedView } from './views/saved';
import './features';

document.title = BRAND.displayName;
const root = document.getElementById('app')!;
let current: { dispose?: () => void } | null = null;
let lastBase: string | null = null;
let lastView: { el: HTMLElement; dispose?: () => void } | null = null;

function render() {
  const route = parseRoute(location.hash);
  // 同じ建物の詳細内でアクションを切り替える時も、シンプルに作り直す（状態はAPIキャッシュに任せる）
  const base = route.name === 'building' ? `b:${route.id}:${route.action ?? ''}` : route.name;
  if (base === lastBase && lastView) return;
  current?.dispose?.();
  clear(root);
  let v: { el: HTMLElement; dispose?: () => void };
  switch (route.name) {
    case 'look':
      v = lookView();
      break;
    case 'map':
      v = mapView();
      break;
    case 'saved':
      v = savedView();
      break;
    case 'building':
      v = detailView(route.id, route.action);
      break;
    default:
      v = { el: homeView() };
  }
  root.append(v.el);
  current = v;
  lastBase = base;
  lastView = v;
}

window.addEventListener('hashchange', render);
render();
