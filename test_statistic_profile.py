"""内存行为验证；SQLite 只作可移植 SQL 语义对照，不代表 MySQL 集成测试。"""
import copy
import json
from pathlib import Path
import sqlite3
import unittest

from statistic_profile import statistic_profile_detail as statistic_profile, distribution


def cube():
    return {'dimensions': [{'name': 'channel', 'filters': []}], 'metrics': [
        {'id': 'amount', 'result_column': 'amount', 'aggregation': 'SUM'},
        {'id': 'rate', 'result_column': 'rate', 'aggregation': 'EXPRESSION'},
    ], 'time': {'name': 'time', 'grain': 'hour', 'timezone': 'Asia/Shanghai', 'filters': []}}


def rows():
    return [
        {'channel': 'A', 'time': '2026-08-01 09:00:00', 'amount': 10, 'rate': 1},
        {'channel': 'B', 'time': '2026-08-01 09:00:00', 'amount': 30, 'rate': 1 / 9},
    ]


class StatisticProfileTests(unittest.TestCase):
    def test_ratio_is_described_without_components_or_business_total(self):
        source, spec = rows(), cube()
        original = copy.deepcopy((source, spec))
        p = statistic_profile(source, spec)
        self.assertAlmostEqual(p['metrics']['rate']['distribution']['mean'], (1 + 1 / 9) / 2)
        bucket = p['time']['buckets'][0]
        self.assertEqual(bucket['result_row_count'], 2)
        self.assertEqual(bucket['metrics']['amount']['distribution']['min'], 10)
        self.assertEqual(bucket['metrics']['amount']['distribution']['max'], 30)
        self.assertEqual(bucket['metrics']['amount']['distribution']['mean'], 20)
        self.assertAlmostEqual(bucket['metrics']['rate']['distribution']['mean'], (1 + 1 / 9) / 2)
        for metric in p['metrics'].values():
            self.assertEqual(set(metric), {'column', 'aggregation', 'distribution'})
        self.assertEqual(set(bucket['metrics']['rate']), {'distribution'})
        self.assertEqual((source, spec), original)

    def test_aggregation_metadata_does_not_change_statistics(self):
        baseline = statistic_profile(rows(), cube())
        for aggregation in ('SUM', 'AVG', 'COUNT_DISTINCT', 'EXPRESSION', None):
            spec = cube()
            spec['metrics'][1]['aggregation'] = aggregation
            actual = statistic_profile(rows(), spec)
            self.assertEqual(actual['metrics']['rate']['distribution'], baseline['metrics']['rate']['distribution'])
            self.assertEqual(actual['time'], baseline['time'])

    def test_empty_and_all_null(self):
        p = statistic_profile([], cube())
        self.assertEqual(p['dataset']['result_row_count'], 0)
        self.assertIsNone(p['metrics']['amount']['distribution']['mean'])
        self.assertIsNone(p['dataset']['fields']['amount']['null_ratio'])
        data = rows()
        for row in data:
            row['amount'] = row['time'] = None
        p = statistic_profile(data, cube())
        self.assertEqual(p['time']['null_count'], 2)
        self.assertEqual(p['time']['buckets'], [])

    def test_quantiles_and_numeric_quality(self):
        p = distribution([0, 10, 20, 30, None, float('nan'), float('inf'), '10', True])
        for key, expected in {'p05': 1.5, 'p25': 7.5, 'p50': 15, 'p75': 22.5, 'p95': 28.5}.items():
            self.assertAlmostEqual(p['quantiles'][key], expected)
        self.assertEqual(p['finite_numeric_count'], 4)
        self.assertEqual(p['non_finite_count'], 2)
        self.assertEqual(p['non_numeric_count'], 2)
        self.assertEqual(distribution([-5])['stddev_pop'], 0)
        self.assertEqual(distribution([-5])['negative_count'], 1)
        json.dumps(p, allow_nan=False)

    def test_duplicates_nulls_and_case_sensitivity(self):
        data = rows()
        data[0]['channel'] = None
        data += [dict(data[0])]
        p = statistic_profile(data, cube())
        self.assertEqual(p['dataset']['duplicates']['extra_row_count'], 1)
        self.assertEqual(p['dataset']['duplicates']['duplicate_group_count'], 1)
        self.assertEqual(p['dataset']['fields']['channel']['null_count'], 2)
        p = statistic_profile([{'x': 'A'}, {'x': 'a'}, {'x': 'a '}], {})
        self.assertEqual(p['dataset']['duplicates']['extra_row_count'], 0)

    def test_time_offsets_invalid_and_no_gap_invention(self):
        data = rows()
        data[0]['time'] = '2026-08-01T01:00:00Z'
        data[1]['time'] = '2026-08-15 09:00:00'
        p = statistic_profile(data, cube(), max_time_buckets=1)
        self.assertEqual(p['time']['min'], '2026-08-01 09:00:00')
        self.assertEqual(p['time']['observed_bucket_count'], 2)
        self.assertTrue(p['time']['truncated'])
        data[1]['time'] = 'invalid'
        p = statistic_profile(data, cube())
        self.assertEqual(p['time']['invalid_count'], 1)
        self.assertEqual(p['time']['valid_time_count'], 1)
        self.assertEqual(statistic_profile([], {})['time']['status'], 'not_applicable')

    def test_invalid_inputs_and_non_numeric_values(self):
        data = rows()
        del data[0]['amount']
        with self.assertRaises(ValueError):
            statistic_profile(data, cube())
        with self.assertRaises(ValueError):
            statistic_profile(rows(), cube(), max_time_buckets=0)
        data = rows()
        data[0]['amount'] = '10'
        p = statistic_profile(data, cube())
        self.assertEqual(p['metrics']['amount']['distribution']['non_numeric_count'], 1)
        self.assertEqual(p['metrics']['amount']['distribution']['finite_numeric_count'], 1)

    def test_mock_against_sql(self):
        root = Path(__file__).parent
        data = json.loads((root / 'mock/events.json').read_text())
        spec = json.loads((root / 'mock/cube.json').read_text())
        p = statistic_profile(data, spec)
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE r (revenue REAL, events INTEGER, event_time TEXT)')
            db.executemany('INSERT INTO r VALUES (:revenue,:events,:event_time)', data)
            count, non_null, lo, hi, mean, start, end = db.execute('''
                SELECT COUNT(*), COUNT(revenue), MIN(revenue), MAX(revenue), AVG(revenue),
                MIN(event_time), MAX(event_time) FROM r
            ''').fetchone()
            d = p['metrics']['revenue']['distribution']
            self.assertEqual(count, p['dataset']['result_row_count'])
            self.assertEqual((non_null, lo, hi), (d['non_null_count'], d['min'], d['max']))
            self.assertAlmostEqual(mean, d['mean'])
            self.assertEqual((start, end), (p['time']['min'], p['time']['max']))
            expected_buckets = db.execute('SELECT event_time, COUNT(*), MIN(revenue), MAX(revenue), AVG(revenue) FROM r GROUP BY event_time ORDER BY event_time').fetchall()
            actual_buckets = [(b['time'], b['result_row_count'], b['metrics']['revenue']['distribution']['min'], b['metrics']['revenue']['distribution']['max'], b['metrics']['revenue']['distribution']['mean']) for b in p['time']['buckets']]
            self.assertEqual(expected_buckets, actual_buckets)
            for percent in (5, 25, 50, 75, 95):
                # 同 MySQL 8 窗口方案；SQLite 新版本同样支持这些标准窗口函数。
                q = db.execute('''WITH ranked AS (
                    SELECT revenue x, ROW_NUMBER() OVER(ORDER BY revenue) rn, COUNT(*) OVER() n
                    FROM r WHERE revenue IS NOT NULL
                ), positions AS (SELECT *, 1+(n-1)*? h FROM ranked)
                SELECT MAX(CASE WHEN rn=FLOOR(h) THEN x END) + (MAX(h)-FLOOR(MAX(h))) *
                (MAX(CASE WHEN rn=CEIL(h) THEN x END)-MAX(CASE WHEN rn=FLOOR(h) THEN x END)) FROM positions
                ''', (percent / 100,)).fetchone()[0]
                self.assertAlmostEqual(q, d['quantiles'][f'p{percent:02d}'])
        self.assertEqual(p['dataset']['duplicates']['extra_row_count'], 0)
        self.assertEqual(sum(b['result_row_count'] for b in p['time']['buckets']), len(data))


if __name__ == '__main__':
    unittest.main()
