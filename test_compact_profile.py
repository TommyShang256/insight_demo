"""摘要必须压缩表达而不丢掉全量统计口径；验证分页可回到完整明细。"""
import json
from pathlib import Path
import statistics
import unittest

from compact_profile import json_bytes
from statistic_profile import statistic_profile, statistic_profile_detail
from dimension_profile import dimension_profile, dimension_profile_detail
from query_profile import query_details


def cube():
    return {'dimensions': [{'name': 'd'}],
            'metrics': [{'id': 'x', 'result_column': 'x', 'aggregation': 'AVG'}],
            'time': {'name': 't', 'grain': 'hour', 'timezone': 'Asia/Shanghai'}}


class CompactTests(unittest.TestCase):
    def test_time_windows_use_original_values_and_extreme_evidence(self):
        rows = [{'d': 'a', 't': '2026-08-01 00:00:00', 'x': 0} for _ in range(9)]
        rows += [{'d': 'b', 't': '2026-08-01 01:00:00', 'x': 100}]
        p = statistic_profile(rows, cube(), max_windows=1)
        w = p['time']['windows'][0]
        stats, low, high = w[-1][0]
        self.assertEqual(stats[6], 0)  # 原行中位数；平均两个桶中位数会错误得到 50。
        self.assertEqual(stats[8], 100)
        self.assertEqual(rows[high[1]]['x'], 100)
        self.assertEqual(rows[low[1]]['x'], 0)
        self.assertEqual(w[2:5], [2, 10, False])
        self.assertEqual(p['time']['represented_bucket_count'], 2)

    def test_gap_boundaries_and_budget_never_drop_later_data(self):
        rows = [{'d': 'a', 't': f'2026-08-{day:02d} 00:00:00', 'x': day} for day in range(1, 22)]
        p = statistic_profile(rows, cube(), max_windows=24)
        self.assertTrue(all(not w[4] for w in p['time']['windows']))
        small = statistic_profile(rows, cube(), max_windows=2)
        self.assertEqual(len(small['time']['windows']), 2)
        self.assertTrue(all(w[4] for w in small['time']['windows']))
        self.assertEqual(sum(w[3] for w in small['time']['windows']), 21)
        self.assertTrue(small['time']['windows'][-1][1].startswith('2026-08-21'))

    def test_all_groups_contribute_even_when_not_represented(self):
        rows = [{'d': str(i), 't': '2026-08-01', 'x': i * 100} for i in range(40)]
        p = dimension_profile(rows, cube(), top_k=4)
        item = p['single_dimensions'][0]
        self.assertEqual(item['group_count'], 40)
        self.assertEqual(len(item['representatives']), 4)
        self.assertEqual(item['group_metric_spreads'][0]['median'][2], statistics.median(range(0, 4000, 100)))
        self.assertEqual(item['group_metric_spreads'][0]['median'][-1], 3900)
        self.assertEqual(item['frequency']['singleton_groups'], 40)
        self.assertTrue(item['coverage']['all_groups_used_for_summary'])
        self.assertEqual(item['coverage']['represented_row_count'] + item['coverage']['remaining_row_count'], 40)
        selected_values = [r[-1][0][6] for r in item['representatives']]
        self.assertIn(3900, selected_values)
        self.assertEqual(p, dimension_profile(list(reversed(rows)), cube(), top_k=4))

    def test_empty_null_invalid_time_and_no_dimensions(self):
        p = statistic_profile([], cube())
        self.assertIn('t', p['dataset']['fields'])
        self.assertEqual(p['time']['windows'], [])
        rows = [{'d': None, 't': None, 'x': None}, {'d': 'null', 't': 'bad', 'x': 1}]
        p = statistic_profile(rows, cube())
        self.assertEqual(p['time']['null_count'], 1)
        self.assertEqual(p['time']['invalid_count'], 1)
        self.assertEqual(dimension_profile(rows, cube())['single_dimensions'][0]['group_count'], 2)
        self.assertEqual(dimension_profile([], {})['status'], 'not_applicable')
        with self.assertRaises(ValueError):
            statistic_profile(rows, cube(), max_windows=0)

    def test_pagination_null_filter_and_metric_projection(self):
        rows = [{'d': i, 't': '2026-08-01', 'x': i} for i in range(13)]
        rows += [{'d': None, 't': '2026-08-02', 'x': None}]
        offset, seen, hashes = 0, [], set()
        while True:
            page = query_details(rows, cube(), dimensions=['d'], offset=offset, limit=3)
            seen += [g['slice'][0]['value'] for g in page['items']]
            hashes.add(page['data_sha256'])
            if not page['has_more']:
                break
            self.assertGreater(page['next_offset'], offset)
            offset = page['next_offset']
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), set(range(13)) | {None})
        self.assertEqual(len(hashes), 1)
        page = query_details(rows, cube(), dimensions=['d'], filters=[{'name': 'd', 'type': 'null', 'value': None}], metrics=[])
        self.assertEqual(page['items'][0]['result_row_share'], 1 / 14)
        self.assertEqual(page['items'][0]['metrics'], {})
        times = query_details(rows, cube(), kind='time', start='2026-08-02', end='2026-08-03')
        self.assertEqual(times['scope']['matched_result_rows'], 1)
        with self.assertRaises(ValueError):
            query_details(rows, cube(), dimensions=['missing'])

    def test_hard_budget_and_mock_compression(self):
        root = Path(__file__).parent
        rows = json.loads((root/'mock/events.json').read_text())
        spec = json.loads((root/'mock/cube.json').read_text())
        s, d = statistic_profile(rows, spec), dimension_profile(rows, spec)
        for p in (s, d):
            self.assertLessEqual(json_bytes(p), 16000)
            self.assertEqual(p['output_budget']['measured_bytes'], json_bytes(p))
        self.assertLess(json_bytes(s), json_bytes(statistic_profile_detail(rows, spec)) * .1)
        self.assertLess(json_bytes(d), json_bytes(dimension_profile_detail(rows, spec)) * .25)
        smaller = dimension_profile(rows, spec, max_bytes=8000)
        self.assertLessEqual(json_bytes(smaller), 8000)
        self.assertTrue(smaller['output_budget']['reduced'])
        self.assertEqual(smaller['single_dimensions'][1]['group_metric_spreads'], d['single_dimensions'][1]['group_metric_spreads'])
        short = statistic_profile(rows, spec, max_bytes=6500)
        self.assertLessEqual(json_bytes(short), 6500)
        self.assertEqual(short['time']['represented_bucket_count'], 279)
        self.assertEqual(sum(w[3] for w in short['time']['windows']), 279)
        with self.assertRaises(ValueError):
            dimension_profile(rows, spec, max_bytes=10)
        detail = query_details(rows, spec, dimensions=['product_id'], limit=10, max_bytes=6000)
        self.assertLessEqual(json_bytes(detail), 6000)
        self.assertTrue(detail['has_more'])
        self.assertGreater(detail['next_offset'], 0)


if __name__ == '__main__':
    unittest.main()
