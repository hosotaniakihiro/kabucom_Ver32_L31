# ============================================================
# File   : trading/backtest_replay/auto_parameter_evolution_v1.py
# Version: Ver01-AUTO-PARAMETER-EVOLUTION
# ------------------------------------------------------------
# 過去の Multi-Day Learning / Daily Learning 結果から、
# 良いパラメータを残し、悪いパラメータを淘汰して、
# 次回探索候補を自動生成する。
# 実運用設定は直接変更しない。
# 出力先: audit/parameter_evolution_YYYYMMDD.json / csv
# ============================================================

from __future__ import annotations

import json
import os
import random
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable, Optional

import pandas as pd

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit'


@dataclass
class EvolutionConfig:
    elite_count: int = 10
    child_count: int = 30
    mutation_rate: float = 0.25
    min_stop_loss_pct: float = 0.10
    max_stop_loss_pct: float = 0.50
    min_trail_drop_pct: float = 0.05
    max_trail_drop_pct: float = 0.50
    min_ai_confidence: float = 0.50
    max_ai_confidence: float = 0.80
    min_volume_floor: float = 30000.0
    max_volume_floor: float = 200000.0
    min_turnover_floor: float = 10000000.0
    max_turnover_floor: float = 100000000.0


class AutoParameterEvolution:
    def __init__(self, source_csv_paths: Iterable[str], output_date: Optional[str] = None, config: Optional[EvolutionConfig] = None):
        self.source_csv_paths = list(source_csv_paths)
        self.output_date = output_date or datetime.now().strftime('%Y%m%d')
        self.config = config or EvolutionConfig()

    def _out_csv(self) -> str:
        os.makedirs(BASE_DIR, exist_ok=True)
        return os.path.join(BASE_DIR, f'parameter_evolution_{self.output_date}.csv')

    def _out_json(self) -> str:
        os.makedirs(BASE_DIR, exist_ok=True)
        return os.path.join(BASE_DIR, f'parameter_evolution_{self.output_date}.json')

    def load_sources(self) -> pd.DataFrame:
        frames = []
        for p in self.source_csv_paths:
            try:
                if os.path.exists(p):
                    df = pd.read_csv(p)
                    df['source_csv'] = p
                    frames.append(df)
            except Exception:
                continue
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True, sort=False)
        return df

    @staticmethod
    def _num(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
        if col not in df.columns:
            return pd.Series([default] * len(df), index=df.index)
        return pd.to_numeric(df[col], errors='coerce').fillna(default)

    def rank(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        x = df.copy()

        # daily learning は score、multi-day は stability_score を持つ。
        score = self._num(x, 'stability_score', 0.0)
        if score.abs().sum() == 0:
            score = self._num(x, 'score', 0.0)
        if score.abs().sum() == 0:
            score = self._num(x, 'gross_pnl', 0.0) + self._num(x, 'total_pnl', 0.0)

        x['evolution_score'] = score
        x = x.sort_values('evolution_score', ascending=False).reset_index(drop=True)
        return x

    def _clip(self, v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, float(v)))

    def _mutate_float(self, value: float, step: float, lo: float, hi: float) -> float:
        if random.random() > self.config.mutation_rate:
            return self._clip(value, lo, hi)
        return self._clip(value + random.choice([-step, step]), lo, hi)

    def _mutate_int(self, value: int, step: int, lo: int, hi: int) -> int:
        if random.random() > self.config.mutation_rate:
            return max(lo, min(hi, int(value)))
        return max(lo, min(hi, int(value + random.choice([-step, step]))))

    def _row_param(self, row: pd.Series, name: str, default):
        try:
            v = row.get(name, default)
            if pd.isna(v):
                return default
            return v
        except Exception:
            return default

    def generate_children(self, ranked: pd.DataFrame) -> pd.DataFrame:
        if ranked.empty:
            return pd.DataFrame()

        elites = ranked.head(self.config.elite_count).copy()
        children = []

        for i in range(self.config.child_count):
            parent = elites.iloc[i % len(elites)]

            stop_loss = float(self._row_param(parent, 'stop_loss_pct', 0.30))
            trail = float(self._row_param(parent, 'trail_drop_pct', 0.30))
            stagnation = int(float(self._row_param(parent, 'stagnation_seconds', 300)))
            conf = float(self._row_param(parent, 'ai_confidence_min', 0.55))
            min_volume = float(self._row_param(parent, 'min_volume', 30000.0))
            min_turnover = float(self._row_param(parent, 'min_turnover', 10000000.0))

            child = {
                'generation_date': self.output_date,
                'parent_rank': int(i % len(elites)),
                'parent_score': float(parent.get('evolution_score', 0.0)),
                'stop_loss_pct': round(self._mutate_float(stop_loss, 0.05, self.config.min_stop_loss_pct, self.config.max_stop_loss_pct), 4),
                'trail_drop_pct': round(self._mutate_float(trail, 0.05, self.config.min_trail_drop_pct, self.config.max_trail_drop_pct), 4),
                'stagnation_seconds': self._mutate_int(stagnation, 60, 60, 600),
                'ai_confidence_min': round(self._mutate_float(conf, 0.05, self.config.min_ai_confidence, self.config.max_ai_confidence), 4),
                'min_volume': round(self._mutate_float(min_volume, 20000.0, self.config.min_volume_floor, self.config.max_volume_floor), 0),
                'min_turnover': round(self._mutate_float(min_turnover, 10000000.0, self.config.min_turnover_floor, self.config.max_turnover_floor), 0),
                'source': 'auto_parameter_evolution_v1',
            }
            children.append(child)

        out = pd.DataFrame(children)
        out = out.drop_duplicates(
            subset=['stop_loss_pct', 'trail_drop_pct', 'stagnation_seconds', 'ai_confidence_min', 'min_volume', 'min_turnover']
        ).reset_index(drop=True)
        return out

    def run(self) -> dict:
        src = self.load_sources()
        ranked = self.rank(src)
        children = self.generate_children(ranked)

        csv_path = self._out_csv()
        json_path = self._out_json()

        if not children.empty:
            children.to_csv(csv_path, index=False, encoding='utf-8-sig')

        result = {
            'ok': not children.empty,
            'output_date': self.output_date,
            'source_csv_paths': self.source_csv_paths,
            'source_rows': int(len(src)) if src is not None else 0,
            'elite_count': self.config.elite_count,
            'child_count': int(len(children)) if children is not None else 0,
            'csv_path': csv_path,
            'json_path': json_path,
            'config': asdict(self.config),
            'best_parent': ranked.iloc[0].to_dict() if ranked is not None and not ranked.empty else {},
            'note': '次回探索候補です。実運用設定は直接変更しません。',
        }

        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)

        return result


def evolve_parameters(source_csv_paths: Iterable[str], output_date: Optional[str] = None) -> dict:
    return AutoParameterEvolution(source_csv_paths=source_csv_paths, output_date=output_date).run()
