import * as THREE from 'three';
import type { RebuildPlan } from '@ie-miru/domain';

/**
 * 建て替えの簡易ボリューム（箱＋屋根）を Three.js で描画する。カメラ映像の上に透過で重ね、
 * 端末の方位変化に合わせて視点を回す（簡易 AR）。既存 3D 資産（Madori3D/sumai3d 等）が使える場合は
 * domain の MassingModelProvider でモデルを差し替える。
 */
export function createMassingScene(canvas: HTMLCanvasElement) {
  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(60, 1, 0.1, 500);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 1.6));
  const sun = new THREE.DirectionalLight(0xffffff, 1.4);
  sun.position.set(10, 20, 8);
  scene.add(sun);
  const ground = new THREE.Mesh(new THREE.CircleGeometry(30, 48), new THREE.MeshBasicMaterial({ color: 0x0f766e, transparent: true, opacity: 0.12 }));
  ground.rotation.x = -Math.PI / 2;
  scene.add(ground);
  let model: THREE.Group | null = null;
  let distance = 18;
  let lookY = 3;
  let yawOffset = 0;
  let last: [number, number] = [0, 5];
  let raf = 0;

  function build(plan: RebuildPlan) {
    if (model) scene.remove(model);
    model = new THREE.Group();
    const floorH = (plan.heightM - (plan.roof === 'gable' ? 1.8 : 0.6)) / plan.floors;
    for (let i = 0; i < plan.floors; i++) {
      const mat = new THREE.MeshStandardMaterial({ color: plan.preset === 'rental_combo' && i === 0 ? 0xc7d2fe : 0xf5f5f4, transparent: true, opacity: 0.88 });
      const box = new THREE.Mesh(new THREE.BoxGeometry(plan.widthM, floorH * 0.98, plan.depthM), mat);
      box.position.y = floorH * i + floorH / 2;
      model.add(box);
      const edges = new THREE.LineSegments(new THREE.EdgesGeometry(box.geometry), new THREE.LineBasicMaterial({ color: 0x0f3d3e }));
      edges.position.copy(box.position);
      model.add(edges);
    }
    const top = floorH * plan.floors;
    if (plan.roof === 'gable') {
      const shape = new THREE.Shape();
      shape.moveTo(-plan.widthM / 2 - 0.3, 0);
      shape.lineTo(plan.widthM / 2 + 0.3, 0);
      shape.lineTo(0, 1.8);
      shape.closePath();
      const roof = new THREE.Mesh(new THREE.ExtrudeGeometry(shape, { depth: plan.depthM + 0.6, bevelEnabled: false }), new THREE.MeshStandardMaterial({ color: 0x57534e }));
      roof.position.set(0, top, -plan.depthM / 2 - 0.3);
      model.add(roof);
    } else {
      const slab = new THREE.Mesh(new THREE.BoxGeometry(plan.widthM + 0.2, 0.6, plan.depthM + 0.2), new THREE.MeshStandardMaterial({ color: 0x78716c }));
      slab.position.y = top + 0.3;
      model.add(slab);
    }
    scene.add(model);
    // 建物全体が画面に収まる距離（縦持ちの狭い横画角を考慮）
    distance = Math.max(16, Math.max(plan.widthM, plan.depthM) * 2.4, plan.heightM * 2.6);
    lookY = plan.heightM * 0.45;
    setView(...last);
  }

  function resize() {
    const w = canvas.clientWidth || 300;
    const h = canvas.clientHeight || 300;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }

  function setView(headingDeltaDeg: number, pitchDeg: number) {
    last = [headingDeltaDeg, pitchDeg];
    yawOffset = (headingDeltaDeg * Math.PI) / 180;
    const pitch = (Math.max(-30, Math.min(30, pitchDeg)) * Math.PI) / 180;
    camera.position.set(0, 1.5, distance);
    camera.lookAt(Math.sin(yawOffset) * distance, lookY + Math.tan(pitch) * distance, distance - Math.cos(yawOffset) * distance);
  }

  function loop() {
    resize();
    renderer.render(scene, camera);
    raf = requestAnimationFrame(loop);
  }
  setView(0, 5);
  loop();
  return {
    build,
    setView,
    dispose() {
      cancelAnimationFrame(raf);
      renderer.dispose();
    },
  };
}
