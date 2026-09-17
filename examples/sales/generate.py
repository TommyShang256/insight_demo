"""Generate a small, deterministic review dataset; no external dependencies."""
import json
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

rng = random.Random(42)
rows = []
for day in range(21):
    for i in range(20):
        channel = ['organic', 'ads', 'referral'][rng.randrange(3)]
        region = ['east', 'west', 'north'][rng.randrange(3)]
        amount = rng.randint(50, 300)
        if day >= 10 and channel == 'ads':
            amount *= 2
        if day == 15 and region == 'east':
            amount *= 3
        rows.append(dict(event_id=f'e{len(rows):04d}', event_time=(datetime(2026, 8, 1) + timedelta(days=day, hours=i)).isoformat(sep=' ', timespec='seconds'), channel=channel, region=region, product_id=f'p{rng.randrange(80):03d}', revenue=amount, converted=int(rng.random() < 0.3)))
rows[7]['channel'] = None
rows[13]['revenue'] = None
rows[21]['revenue'] = 0
rows[36]['revenue'] = -80
rows[55]['event_time'] = None
rows.extend([dict(rows[100]), dict(rows[200])])
assert len(rows) == 422
assert len({json.dumps(r, sort_keys=True) for r in rows}) == 420
out = Path(__file__).parent
time_ranges = [('2026-08-01 00:00:00', '2026-08-08 00:00:00'), ('2026-08-15 00:00:00', '2026-08-22 00:00:00')]
time_filters = [f"event_time >= '{start}' AND event_time < '{end}'" for start, end in time_ranges]
time_where = ' OR '.join(f'({condition})' for condition in time_filters)
sql = f"""SELECT event_time, channel, region, product_id,
       SUM(revenue) AS revenue, COUNT(*) AS events,
       1.0 * SUM(converted) / NULLIF(COUNT(*), 0) AS conversion_rate
FROM mock_events
WHERE ({time_where})
GROUP BY event_time, channel, region, product_id
ORDER BY event_time, channel, region, product_id"""
with sqlite3.connect(":memory:") as db:
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE mock_events (event_id TEXT, event_time TEXT, channel TEXT, region TEXT, product_id TEXT, revenue REAL, converted INTEGER)")
    db.executemany("INSERT INTO mock_events VALUES (:event_id, :event_time, :channel, :region, :product_id, :revenue, :converted)", rows)
    result = [dict(row) for row in db.execute(sql)]
filtered_rows = [r for r in rows if r['event_time'] is not None and any(start <= r['event_time'] < end for start, end in time_ranges)]
assert sum(r['events'] for r in result) == len(filtered_rows)
assert all(any(start <= r['event_time'] < end for start, end in time_ranges) for r in result)
assert len({tuple(r[k] for k in ['event_time', 'channel', 'region', 'product_id']) for r in result}) == len(result)
(out / 'query_result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
(out / 'query.sql').write_text(sql + ';\n')
cube = {
    'dimensions': [
        {'id': 'channel', 'name': 'channel', 'labelEn': 'Channel', 'labelCh': '渠道', 'filters': []},
        {'id': 'region', 'name': 'region', 'labelEn': 'Region', 'labelCh': '地区', 'filters': []},
        {'id': 'product', 'name': 'product_id', 'labelEn': 'Product', 'labelCh': '商品', 'filters': []},
    ],
    'metrics': [
        {'id': 'revenue', 'name': '收入', 'result_column': 'revenue', 'aggregation': 'SUM', 'unit': 'CNY'},
        {'id': 'events', 'name': '事件数', 'result_column': 'events', 'aggregation': 'COUNT', 'unit': '条'},
        {'id': 'conversion_rate', 'name': '转化率', 'result_column': 'conversion_rate', 'aggregation': 'EXPRESSION', 'unit': 'ratio'},
    ],
    'time': {'id': 'event_time', 'name': 'event_time', 'labelEn': 'Event Time', 'labelCh': '事件时间', 'filters': time_filters, 'grain': 'hour', 'timezone': 'Asia/Shanghai'},
    'note': '输入是 query.sql 的实际结果。filters 为空表示不限制；SQL 已执行，profiling 不重复过滤。aggregation 仅说明原 SQL 的聚合类型，profile 直接描述返回值。',
}
(out / 'cube.json').write_text(json.dumps(cube, ensure_ascii=False, indent=2) + '\n')
print(f'Generated {len(result)} SQL result rows from 422 internal fixture rows; aggregate checks passed.')
