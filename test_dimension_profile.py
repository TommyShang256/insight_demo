"""验证维度切片口径、截断覆盖、类型边界，并独立对照 SQL 分组统计。"""
import copy
import json
from pathlib import Path
import sqlite3
import unittest

from dimension_profile import dimension_profile_detail as dimension_profile


def cube():
    return {'dimensions': [{'name': 'channel'}, {'name': 'region'}],
            'metrics': [{'id': 'revenue', 'result_column': 'revenue', 'aggregation': 'SUM'}],
            'time': {'name': 'time', 'timezone': 'Asia/Shanghai', 'grain': 'day'}}


def rows():
    return [
        {'channel': 'ads', 'region': 'east', 'time': '2026-08-01', 'revenue': 100},
        {'channel': 'organic', 'region': 'west', 'time': '2026-08-01', 'revenue': 900},
        {'channel': 'ads', 'region': 'east', 'time': '2026-08-02', 'revenue': 200},
        {'channel': 'ads', 'region': None, 'time': '2026-08-03', 'revenue': 300},
    ]


class DimensionProfileTests(unittest.TestCase):
    def test_row_share_not_metric_contribution(self):
        data, spec = rows(), cube()
        before = copy.deepcopy((data, spec))
        profile = dimension_profile(data, spec)
        channel = profile['single_dimensions'][0]
        ads = channel['groups'][0]
        self.assertEqual(channel['non_null_group_count'], 2)
        self.assertEqual(ads['result_row_count'], 3)
        self.assertEqual(ads['result_row_share'], .75)
        self.assertEqual(ads['metrics']['revenue']['distribution']['mean'], 200)
        self.assertEqual(set(ads['metrics']['revenue']), {'column', 'aggregation', 'distribution'})
        self.assertEqual(ads['time']['distinct_time_count'], 3)
        self.assertEqual(ads['time']['max'], '2026-08-03 00:00:00')
        self.assertEqual((data, spec), before)

    def test_top_k_and_pair_nulls_preserve_denominator(self):
        profile = dimension_profile(rows(), cube(), top_k=1)
        channel = profile['single_dimensions'][0]
        self.assertEqual(channel['coverage']['remaining_row_count'], 1)
        self.assertEqual(channel['coverage']['remaining_row_share'], .25)
        pair = profile['combinations'][0]
        self.assertEqual(pair['group_count'], 3)
        self.assertEqual(pair['non_null_group_count'], 2)
        self.assertEqual(pair['null_row_count'], 1)
        self.assertEqual(pair['null_row_share'], .25)
        self.assertEqual(pair['groups'][0]['result_row_share'], .5)
        for item in profile['single_dimensions'] + profile['combinations']:
            self.assertEqual(sum(g['result_row_count'] for g in item['groups']) + item['coverage']['remaining_row_count'], 4)
            self.assertEqual(sum(g['result_row_share'] for g in item['groups']) + item['coverage']['remaining_row_share'], 1)

    def test_null_empty_string_and_typed_values(self):
        data = [{'d': v} for v in [None, None, '', 'null', 'Other', True, 1, 1.0, '1']]
        profile = dimension_profile(data, {'dimensions': [{'name': 'd'}]}, top_k=20)
        item = profile['single_dimensions'][0]
        self.assertEqual(item['group_count'], 7)
        self.assertEqual(item['non_null_group_count'], 6)
        self.assertEqual(item['null_row_count'], 2)
        self.assertEqual(item['groups'][0]['result_row_count'], 2)
        self.assertEqual(profile, dimension_profile(list(reversed(data)), {'dimensions': [{'name': 'd'}]}, top_k=20))
        json.dumps(profile, allow_nan=False)

    def test_no_dimensions_empty_and_all_null(self):
        self.assertEqual(dimension_profile(rows(), {'metrics': []})['status'], 'not_applicable')
        p = dimension_profile([], cube())
        self.assertEqual(p['single_dimensions'][0]['group_count'], 0)
        self.assertIsNone(p['single_dimensions'][0]['null_row_share'])
        self.assertEqual(p['single_dimensions'][0]['groups'], [])
        data = [{'d': None}, {'d': None}]
        item = dimension_profile(data, {'dimensions': [{'name': 'd'}]})['single_dimensions'][0]
        self.assertEqual(item['group_count'], 1)
        self.assertEqual(item['non_null_group_count'], 0)
        self.assertEqual(item['groups'][0]['result_row_share'], 1)
        self.assertEqual(item['groups'][0]['time']['status'], 'not_applicable')

    def test_pair_budget_and_stability(self):
        names = ['a', 'b', 'c', 'd', 'e']
        data = [{n: (i if n == 'a' else 'one') for n in names} for i in range(5)]
        spec = {'dimensions': [{'name': n} for n in reversed(names)]}
        p = dimension_profile(data, spec, max_pairs=2)
        self.assertEqual(p['coverage']['candidate_pair_count'], 10)
        self.assertEqual(p['coverage']['computed_pair_count'], 2)
        self.assertEqual(len(p['coverage']['skipped_pairs']), 8)
        self.assertEqual([x['dimensions'] for x in p['combinations']], [['b', 'c'], ['b', 'd']])
        self.assertEqual(p, dimension_profile(list(reversed(data)), spec, max_pairs=2))
        self.assertEqual(dimension_profile(data, spec, max_pairs=0)['combinations'], [])

    def test_time_offsets_nulls_invalid_and_metric_quality(self):
        data = rows()
        data[0]['time'] = '2026-08-01T01:00:00Z'
        data[2]['time'] = None
        data[3]['time'] = 'bad'
        data[2]['revenue'] = None
        data[3]['revenue'] = float('nan')
        ads = dimension_profile(data, cube())['single_dimensions'][0]['groups'][0]
        self.assertEqual(ads['time']['min'], '2026-08-01 09:00:00')
        self.assertEqual(ads['time']['valid_time_count'], 1)
        self.assertEqual(ads['time']['null_count'], 1)
        self.assertEqual(ads['time']['invalid_count'], 1)
        self.assertEqual(ads['metrics']['revenue']['distribution']['finite_numeric_count'], 1)
        self.assertEqual(ads['metrics']['revenue']['distribution']['non_finite_count'], 1)
        json.dumps(ads, allow_nan=False)

    def test_validation(self):
        for kwargs in ({'top_k': 0}, {'max_pairs': -1}, {'top_k': True}):
            with self.assertRaises(ValueError):
                dimension_profile(rows(), cube(), **kwargs)
        data = rows()
        del data[0]['channel']
        with self.assertRaises(ValueError):
            dimension_profile(data, cube())
        with self.assertRaises(ValueError):
            dimension_profile([{'d': float('nan')}], {'dimensions': [{'name': 'd'}]})
        with self.assertRaises(ValueError):
            dimension_profile([], {'dimensions': [{'name': 'd'}, {'name': 'd'}]})

    def test_group_statistics_against_sql(self):
        root = Path(__file__).parent
        data = json.loads((root / 'mock/events.json').read_text())
        spec = json.loads((root / 'mock/cube.json').read_text())
        p = dimension_profile(data, spec, top_k=1000)
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE r (channel TEXT, region TEXT, product_id TEXT, revenue REAL)')
            db.executemany('INSERT INTO r VALUES (:channel,:region,:product_id,:revenue)', data)
            for item in p['single_dimensions'] + p['combinations']:
                # 标识符来自固定 mock schema，仅测试构造 SQL。
                cols = ','.join(item['dimensions'])
                query = f'SELECT {cols}, COUNT(*),COUNT(revenue),MIN(revenue),MAX(revenue),AVG(revenue) FROM r GROUP BY {cols}'
                sql_groups = db.execute(query).fetchall()
                size = len(item['dimensions'])
                expected = {tuple(row[:size]): row[size:] for row in sql_groups}
                self.assertEqual(item['group_count'], len(expected))
                for group in item['groups']:
                    count, non_null, lo, hi, mean = expected[tuple(v['value'] for v in group['slice'])]
                    dist = group['metrics']['revenue']['distribution']
                    self.assertEqual(group['result_row_count'], count)
                    self.assertEqual(group['result_row_share'], count / len(data))
                    self.assertEqual((dist['non_null_count'], dist['min'], dist['max']), (non_null, lo, hi))
                    if mean is None:
                        self.assertIsNone(dist['mean'])
                    else:
                        self.assertAlmostEqual(dist['mean'], mean)


if __name__ == '__main__':
    unittest.main()
