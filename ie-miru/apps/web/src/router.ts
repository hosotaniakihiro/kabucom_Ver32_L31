export type Route =
  | { name: 'home' }
  | { name: 'look' }
  | { name: 'map' }
  | { name: 'saved' }
  | { name: 'building'; id: string; action: string | null };

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  switch (parts[0]) {
    case 'look':
      return { name: 'look' };
    case 'map':
      return { name: 'map' };
    case 'saved':
      return { name: 'saved' };
    case 'b':
      if (parts[1]) return { name: 'building', id: decodeURIComponent(parts[1]), action: parts[2] ?? null };
      return { name: 'home' };
    default:
      return { name: 'home' };
  }
}

export function go(path: string) {
  location.hash = path.startsWith('#') ? path : `#${path}`;
}

export const buildingPath = (id: string, action?: string) => `/b/${encodeURIComponent(id)}${action ? `/${action}` : ''}`;
