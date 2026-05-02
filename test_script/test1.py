from sqlalchemy import func
from database import Session_ranking
from database.models import RankingRaw1Min

with Session_ranking() as s:
    latest = s.query(func.max(RankingRaw1Min.snapshot_time)).scalar()

    rows = (
        s.query(RankingRaw1Min.symbol)
        .filter(RankingRaw1Min.snapshot_time == latest)
        .distinct()
        .all()
    )

print("latest snapshot:", latest)
print("symbol count:", len(rows))