import { cameraPoseFromDeviceOrientation, smoothHeading, type CameraPose, type LocationFix, type LocationPermission } from '@ie-miru/domain';

/** iOS 13+ は DeviceOrientation に明示許可が必要（ユーザー操作の中で呼ぶこと） */
export async function requestOrientationPermission(): Promise<'granted' | 'denied' | 'unsupported'> {
  const DOE = (window as any).DeviceOrientationEvent;
  if (!DOE) return 'unsupported';
  if (typeof DOE.requestPermission === 'function') {
    try {
      return (await DOE.requestPermission()) === 'granted' ? 'granted' : 'denied';
    } catch {
      return 'denied';
    }
  }
  return 'granted';
}

export function watchOrientation(onPose: (p: CameraPose) => void): () => void {
  let smoothed: number | null = null;
  const handler = (e: DeviceOrientationEvent) => {
    const anyE = e as DeviceOrientationEvent & { webkitCompassHeading?: number; webkitCompassAccuracy?: number };
    const pose = cameraPoseFromDeviceOrientation({
      alpha: e.alpha,
      beta: e.beta,
      gamma: e.gamma,
      absolute: e.absolute || e.type === 'deviceorientationabsolute',
      webkitCompassHeading: anyE.webkitCompassHeading ?? null,
      webkitCompassAccuracy: anyE.webkitCompassAccuracy ?? null,
    });
    if (!pose) return;
    smoothed = smoothHeading(smoothed, pose.heading, 0.3);
    onPose({ ...pose, heading: smoothed, yaw: smoothed > 180 ? smoothed - 360 : smoothed });
  };
  window.addEventListener('deviceorientationabsolute', handler as EventListener);
  window.addEventListener('deviceorientation', handler as EventListener);
  return () => {
    window.removeEventListener('deviceorientationabsolute', handler as EventListener);
    window.removeEventListener('deviceorientation', handler as EventListener);
  };
}

export interface GeoState {
  permission: LocationPermission;
  fix: LocationFix | null;
  error: string | null;
}

export function watchGeolocation(onState: (s: GeoState) => void): () => void {
  if (!('geolocation' in navigator)) {
    onState({ permission: 'unsupported', fix: null, error: 'unsupported' });
    return () => {};
  }
  const id = navigator.geolocation.watchPosition(
    (pos) =>
      onState({
        permission: 'granted',
        error: null,
        fix: {
          latitude: pos.coords.latitude,
          longitude: pos.coords.longitude,
          horizontalAccuracy: pos.coords.accuracy ?? -1,
          altitude: pos.coords.altitude ?? null,
          heading: pos.coords.heading != null && !Number.isNaN(pos.coords.heading) ? pos.coords.heading : null,
          headingAccuracy: null,
          timestamp: pos.timestamp || Date.now(),
        },
      }),
    (err) => onState({ permission: err.code === err.PERMISSION_DENIED ? 'denied' : 'granted', fix: null, error: err.code === err.TIMEOUT ? 'timeout' : err.message || 'error' }),
    { enableHighAccuracy: true, maximumAge: 2000, timeout: 20000 },
  );
  return () => navigator.geolocation.clearWatch(id);
}

export async function startCamera(video: HTMLVideoElement): Promise<{ ok: true; stop: () => void } | { ok: false; reason: 'denied' | 'unsupported' | 'error' }> {
  if (!navigator.mediaDevices?.getUserMedia) return { ok: false, reason: 'unsupported' };
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' } }, audio: false });
    video.srcObject = stream;
    video.setAttribute('playsinline', '');
    video.muted = true;
    await video.play().catch(() => {});
    return { ok: true, stop: () => stream.getTracks().forEach((t) => t.stop()) };
  } catch (e) {
    const name = (e as DOMException)?.name;
    return { ok: false, reason: name === 'NotAllowedError' || name === 'SecurityError' ? 'denied' : 'error' };
  }
}

/** 背面カメラの縦持ち時の水平画角（概算）。端末差があるため候補マーカーの目安表示にのみ使う */
export const CAMERA_HFOV_DEG = 50;
