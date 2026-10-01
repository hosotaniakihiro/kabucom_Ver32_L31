import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import { DEFAULT_MAP_CENTER } from '@ie-miru/config';
import type { Building } from '@ie-miru/domain';
import { api } from '../api';
import { h } from '../dom';
import { buildingPath, go } from '../router';
import { appState } from '../state';

/** 地図から建物を手動選択する（位置情報が使えない・精度が悪いときの受け皿） */
export function mapView(): { el: HTMLElement; dispose: () => void } {
  const mapEl = h('div', { class: 'map', 'data-testid': 'map' });
  const status = h('p', { class: 'map-status', 'data-testid': 'map-status' }, '建物をタップして選んでください。');
  const el = h('main', { class: 'mapview' }, h('header', { class: 'bar' }, h('button', { class: 'ghost', onclick: () => go('/') }, '←'), h('h1', {}, 'マップ')), mapEl, status);

  const start = appState.lastPosition ?? DEFAULT_MAP_CENTER;
  let map: L.Map | null = null;
  const layer = L.layerGroup();
  let timer: ReturnType<typeof setTimeout> | null = null;

  requestAnimationFrame(() => {
    map = L.map(mapEl, { zoomControl: true }).setView([start.lat, start.lng], 18);
    L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png', {
      maxZoom: 19,
      maxNativeZoom: 18,
      attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html" target="_blank" rel="noopener">国土地理院</a>',
    }).addTo(map);
    layer.addTo(map);
    map.on('moveend', () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(load, 300);
    });
    if (!appState.lastPosition && 'geolocation' in navigator) {
      navigator.geolocation.getCurrentPosition(
        (p) => map?.setView([p.coords.latitude, p.coords.longitude], 18),
        () => (status.textContent = '位置情報が使えないため、地図を動かして建物を探してください。'),
        { enableHighAccuracy: true, timeout: 8000 },
      );
    }
    load();
  });

  async function load() {
    if (!map) return;
    const c = map.getCenter();
    try {
      const r = await api.nearby(c.lat, c.lng, 150);
      appState.remember(r.buildings);
      draw(r.buildings);
      const demo = r.sources?.some((s: any) => s.mode === 'demo');
      status.textContent = r.status === 'error' ? '建物データを取得できませんでした（確認できず）。' : r.buildings.length === 0 ? 'この付近の建物データがありません。' : `建物をタップして選んでください（${r.buildings.length}件）${demo ? '　※デモ建物データ' : ''}`;
    } catch {
      status.textContent = '通信できませんでした。';
    }
  }

  function draw(list: Building[]) {
    layer.clearLayers();
    for (const b of list) {
      const poly = L.polygon(
        b.footprint.map((p) => [p.lat, p.lng] as [number, number]),
        { color: '#0f766e', weight: 1, fillOpacity: 0.25, className: 'bpoly' },
      );
      poly.on('click', () => go(buildingPath(b.id)));
      (poly as any).options.buildingId = b.id;
      poly.addTo(layer);
      poly.getElement?.()?.setAttribute('data-building-id', b.id);
    }
  }

  return {
    el,
    dispose: () => {
      if (timer) clearTimeout(timer);
      map?.remove();
    },
  };
}
