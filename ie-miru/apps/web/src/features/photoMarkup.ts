import type { PhotoMark } from '@ie-miru/domain';
import { h } from '../dom';

/**
 * 写真へのマーキング。タップした位置に番号付きの円を置く（座標は 0..1 に正規化して保存）。
 */
export function photoMarkup() {
  const canvas = h('canvas', { class: 'markup', 'data-testid': 'markup-canvas', width: 10, height: 10 }) as HTMLCanvasElement;
  const marks: PhotoMark[] = [];
  let img: HTMLImageElement | null = null;
  let onChange: () => void = () => {};

  function draw() {
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (img) ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    else {
      ctx.fillStyle = '#ddd';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
    }
    marks.forEach((m, i) => {
      const r = Math.max(10, m.r * canvas.width);
      ctx.lineWidth = Math.max(3, canvas.width / 200);
      ctx.strokeStyle = '#ef4444';
      ctx.beginPath();
      ctx.arc(m.x * canvas.width, m.y * canvas.height, r, 0, Math.PI * 2);
      ctx.stroke();
      ctx.fillStyle = '#ef4444';
      ctx.font = `bold ${Math.max(14, canvas.width / 30)}px sans-serif`;
      ctx.fillText(String(i + 1), m.x * canvas.width + r + 4, m.y * canvas.height - r / 2);
    });
  }

  canvas.addEventListener('click', (e) => {
    const rect = canvas.getBoundingClientRect();
    if (rect.width === 0 || !img) return;
    marks.push({ x: (e.clientX - rect.left) / rect.width, y: (e.clientY - rect.top) / rect.height, r: 0.05, label: null });
    draw();
    onChange();
  });

  return {
    canvas,
    marks,
    hasImage: () => !!img,
    onChange(fn: () => void) {
      onChange = fn;
    },
    undo() {
      marks.pop();
      draw();
      onChange();
    },
    async loadFile(file: File) {
      const url = URL.createObjectURL(file);
      const im = new Image();
      await new Promise<void>((res, rej) => {
        im.onload = () => res();
        im.onerror = () => rej(new Error('image load failed'));
        im.src = url;
      });
      const max = 1600;
      const scale = Math.min(1, max / Math.max(im.naturalWidth, im.naturalHeight));
      canvas.width = Math.max(1, Math.round(im.naturalWidth * scale));
      canvas.height = Math.max(1, Math.round(im.naturalHeight * scale));
      img = im;
      marks.length = 0;
      draw();
      onChange();
    },
    /** 縮小した元写真（マーキングは焼き込まず、座標で別保存） */
    async exportPhoto(): Promise<Blob | null> {
      if (!img) return null;
      const c = document.createElement('canvas');
      c.width = canvas.width;
      c.height = canvas.height;
      c.getContext('2d')!.drawImage(img, 0, 0, c.width, c.height);
      return new Promise((res) => c.toBlob((b) => res(b), 'image/jpeg', 0.85));
    },
  };
}
