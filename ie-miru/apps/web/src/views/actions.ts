import type { Building } from '@ie-miru/domain';

export interface ActionContext {
  report: any;
  building: Building;
  close: () => void;
}

export interface ActionDef {
  /** 全画面パネル（買う/売る/直す/貸す/建て替える） */
  render?: (ctx: ActionContext) => { el: HTMLElement; dispose?: () => void };
  /** ヘッダ等に差し込む小さな UI（保存ボタン） */
  inline?: (ctx: ActionContext) => HTMLElement;
  /** 詳細内のセクション差し替え（参考相場など） */
  section?: (report: any) => { summary: string; body: HTMLElement[] };
}

/** 各 Phase の機能はここへ登録する（未登録のボタンは無効表示） */
export const actionRegistry = new Map<string, ActionDef>();
