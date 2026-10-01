import {
  DATA_STATUS_LABEL, HAZARD_LABEL, PROVENANCE_LABEL, hazardStatusExplanation, hazardStatusLabel,
  type DataMode, type HazardResult, type ProvenanceKind, type Sourced, type SourceRef,
} from '@ie-miru/domain';
import { BRAND } from '@ie-miru/config';

/** 値セルの表示モデル（Web / iOS 共通） */
export interface ValueCell {
  text: string;
  /** 値が無い場合 true（「データなし」等を薄く表示） */
  empty: boolean;
  badge: { kind: ProvenanceKind; label: string };
  note: string | null;
  mock: boolean;
}

export function cell<T>(s: Sourced<T>, fmt: (v: T) => string): ValueCell {
  const mock = s.sources.some((x) => x.mode === 'mock' || x.mode === 'demo');
  if (s.status === 'available') {
    return { text: fmt(s.value), empty: false, badge: badge(s.kind), note: s.note ?? null, mock };
  }
  return { text: DATA_STATUS_LABEL[s.status], empty: true, badge: badge(s.kind), note: s.reason, mock };
}

export function badge(kind: ProvenanceKind) {
  return { kind, label: PROVENANCE_LABEL[kind] };
}

export const MODE_LABEL: Record<DataMode, string> = {
  live: '',
  cache: '（キャッシュ）',
  mock: '【モックデータ・実データではありません】',
  demo: '【デモデータ・実在しません】',
};

export function sourceLine(sources: SourceRef[]): string {
  const uniq = [...new Map(sources.map((s) => [s.id, s])).values()];
  if (uniq.length === 0) return '出典: なし';
  return `出典: ${uniq.map((s) => `${s.name}${MODE_LABEL[s.mode]}`).join(' / ')}`;
}

export interface HazardRow {
  label: string;
  status: string;
  explanation: string;
  severity: number | null;
  tone: 'alert' | 'caution' | 'muted';
}

export function hazardRow(h: HazardResult): HazardRow {
  const tone = h.status === 'in_zone' ? ((h.severity ?? 3) >= 3 ? 'alert' : 'caution') : h.status === 'graded' && (h.severity ?? 0) >= 3 ? 'caution' : 'muted';
  return {
    label: HAZARD_LABEL[h.type],
    status: hazardStatusLabel(h),
    explanation: [hazardStatusExplanation(h), h.detail].filter(Boolean).join(' '),
    severity: h.severity,
    tone,
  };
}

export const UI_TEXT = {
  appName: BRAND.displayName,
  home: { look: '見る', map: 'マップ', saved: '保存した家' },
  about: 'この家について',
  actions: 'この家でできること',
  sections: { market: '参考相場', land: '土地', zoning: '用途地域', hazard: '災害' },
  actionLabels: { buy: '買う', sell: '売る', fix: '直す', rent: '貸す', rebuild: '建て替える' },
  confirm: 'この建物ですか？',
  comparablesTitle: '周辺取引事例',
  marketTitle: '参考相場',
  valuationTitle: 'AI参考査定',
  valuationDisclaimer: 'AI参考査定は公開データからの機械的な推定で、正式な不動産鑑定・査定ではありません。',
  rebuildDisclaimer: '建て替えプランは参考表示です。法規（斜線制限・日影規制・条例等）への適合や「建築可能」であることを示すものではありません。',
  disclaimer: BRAND.disclaimer,
} as const;
