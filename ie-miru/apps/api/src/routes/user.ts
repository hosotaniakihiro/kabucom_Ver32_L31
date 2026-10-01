import type { Hono } from 'hono';
import type { Building, LatLng } from '@ie-miru/domain';
import type { AppDeps, AppEnv } from '../app';

export interface UserRouteDeps extends AppDeps {
  findBuilding(id: string, hint: LatLng | null): Promise<Building | null>;
}

/** 保存した家・修繕記録・ARメモ（Phase 18 以降で実装） */
export function registerUserRoutes(_app: Hono<AppEnv>, _deps: UserRouteDeps) {}
