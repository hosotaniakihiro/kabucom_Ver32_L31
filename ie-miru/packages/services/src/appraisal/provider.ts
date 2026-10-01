/**
 * 不動産会社査定（机上査定・訪問査定）への接続口。特定の会社・一括査定サービスに依存しない。
 * 実接続には各社との契約・個人情報の取り扱い同意が必要なため、既定は「未接続」。
 */
export interface AppraisalRequest {
  buildingId: string;
  /** 町丁レベルまでの所在（番地は送らない） */
  areaLabel: string | null;
  propertyType: string;
  landAreaM2: number | null;
  floorAreaM2: number | null;
  builtYear: number | null;
  /** 利用者が明示同意した連絡手段（未同意なら送らない） */
  contact: { consent: true; channel: 'email' | 'phone'; value: string } | null;
}

export type AppraisalResponse =
  | { status: 'accepted'; providerId: string; referenceId: string; message: string }
  | { status: 'not_connected'; providerId: string; message: string }
  | { status: 'rejected'; providerId: string; message: string };

export interface AppraisalProvider {
  readonly id: string;
  readonly name: string;
  /** 机上査定のみ / 訪問査定可 など */
  readonly capabilities: Array<'desk' | 'visit'>;
  request(req: AppraisalRequest): Promise<AppraisalResponse>;
}

export class NotConnectedAppraisalProvider implements AppraisalProvider {
  readonly id = 'none';
  readonly name = '不動産会社査定（未接続）';
  readonly capabilities: Array<'desk' | 'visit'> = [];
  async request(): Promise<AppraisalResponse> {
    return { status: 'not_connected', providerId: this.id, message: '不動産会社の査定サービスは現在接続されていません。AI参考査定と周辺取引事例を参考にしてください。' };
  }
}

/** 複数社を並列に扱うレジストリ（特定社を優遇しない: 登録順にかかわらず同列に返す） */
export class AppraisalProviderRegistry {
  private readonly providers = new Map<string, AppraisalProvider>();
  register(p: AppraisalProvider) {
    this.providers.set(p.id, p);
    return this;
  }
  list() {
    return [...this.providers.values()].map((p) => ({ id: p.id, name: p.name, capabilities: p.capabilities }));
  }
  async requestAll(req: AppraisalRequest): Promise<AppraisalResponse[]> {
    if (this.providers.size === 0) return [await new NotConnectedAppraisalProvider().request()];
    return Promise.all([...this.providers.values()].map((p) => p.request(req).catch((e) => ({ status: 'rejected' as const, providerId: p.id, message: String(e) }))));
  }
}
