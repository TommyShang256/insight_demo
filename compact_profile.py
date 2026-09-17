"""全量计算、列式输出的委派摘要。统计是描述性的，不合成业务指标。"""
from collections import defaultdict
from datetime import timedelta
from itertools import combinations
import json
from zoneinfo import ZoneInfo

from statistic_profile import distribution, finite_number, parse_time, quantile, statistic_profile_detail
from dimension_profile import validate_input, group_rows, dimension_overview, select_groups


STATS = ['finite_numeric_count', 'null_count', 'non_numeric_count', 'non_finite_count',
         'min', 'p05', 'p50', 'p95', 'max', 'stddev_pop']


def compact_distribution(values):
    """返回固定列顺序的分布。用法：与 STATS 配对读取；原理：复用原分布算法。

    MySQL：COUNT/MIN/MAX/STDDEV_POP 及窗口插值，与原 distribution 注释相同。
    不采样、不把分位数再次求平均；NULL/非法值数量仍保留。
    """
    d = distribution(values)
    return [d['quantiles'][k] if k.startswith('p') else d[k] for k in STATS]


def spread(values):
    """给组级统计生成 [min,P10,P50,P90,max]；空列表各项为 None。

    使用于组大小、每组中位数和标准差；每组等权，不代表原结果行分布。
    原理：排序后 Type 7 插值；MySQL 对组级中间表再运行分位数窗口查询。
    """
    values = sorted(values)
    return [quantile(values, p) for p in (0, .1, .5, .9, 1)]


def metric_schema(cube):
    """将指标定义写一次。用法：所有指标数组按该列表顺序解码。

    原理：仅映射元数据，不执行表达式；SQL 下推无需计算。
    """
    return [{'id': m['id'], 'column': m['result_column'], 'aggregation': m.get('aggregation')}
            for m in cube.get('metrics', [])]


def next_bucket(value, grain):
    """返回下一原粒度桶边界；用于识别未观测间隔，不判断缺失原因。

    hour/day 加日历时长，month 移动到下月首日。MySQL 对应 DATE_ADD。
    输入为 parse_time 统一后的本地时间，延续原实现的夏令时限制。
    """
    if grain == 'month':
        return value.replace(year=value.year + (value.month == 12), month=value.month % 12 + 1)
    return value + timedelta(hours=1) if grain == 'hour' else value + timedelta(days=1)


def time_summary(rows, cube, max_windows):
    """把全部观测桶组织为有限展示窗口，每个窗口直接统计原结果值。

    用法：max_windows 为正整数。优先不跨未观测间隔；连续段多于预算时明确标记
    spans_unobserved_gap，而非截掉剩余时间。保留极值原数组索引和时间以便定位。
    MySQL：按桶产生连续段，给窗口分配标签后 GROUP BY；分位数从原行计算，
    极值证据用 ROW_NUMBER 按值及稳定行号排序获取，不能平均桶分位数。
    """
    time = cube.get('time')
    if not time:
        return {'status': 'not_applicable'}
    grain = time.get('grain')
    formats = {'hour': '%Y-%m-%d %H:00:00', 'day': '%Y-%m-%d', 'month': '%Y-%m-01'}
    if grain not in formats:
        return {'status': 'unavailable', 'reason': 'supported grains: hour/day/month'}
    zone = ZoneInfo(time['timezone']) if time.get('timezone') else None
    groups, parsed = defaultdict(list), {}
    null_count = invalid_count = 0
    for index, row in enumerate(rows):
        raw = row[time['name']]
        if raw is None:
            null_count += 1
            continue
        try:
            value = parse_time(raw, zone)
        except (ValueError, TypeError, OverflowError):
            invalid_count += 1
            continue
        key = parse_time(value.strftime(formats[grain]), None)
        parsed[index] = value.isoformat(sep=' ')
        groups[key].append(index)
    keys = sorted(groups)
    segments = []
    for key in keys:
        if not segments or next_bucket(segments[-1][-1], grain) != key:
            segments.append([])
        segments[-1].append(key)
    windows = []
    if len(segments) > max_windows:
        # 段过多时连续打包段；所有桶仍覆盖，跨间隔标记不能被解释为连续。
        for i in range(max_windows):
            batch = segments[i * len(segments) // max_windows:(i + 1) * len(segments) // max_windows]
            windows.append(([key for segment in batch for key in segment], len(batch) > 1))
    elif segments:
        allocations = [1] * len(segments)
        while sum(allocations) < min(max_windows, len(keys)):
            i = max((i for i in range(len(segments)) if allocations[i] < len(segments[i])),
                    key=lambda i: (len(segments[i]) / allocations[i], -i))
            allocations[i] += 1
        for segment, count in zip(segments, allocations):
            for i in range(count):
                windows.append((segment[i * len(segment) // count:(i + 1) * len(segment) // count], False))
    table = []
    for bucket_keys, gap in windows:
        indices = [i for key in bucket_keys for i in groups[key]]
        metrics = []
        for metric in cube.get('metrics', []):
            column = metric['result_column']
            valid = [i for i in indices if finite_number(rows[i][column])]
            extrema = []
            for choose in (min, max):
                idx = choose(valid, key=lambda i: rows[i][column]) if valid else None
                extrema.append([parsed[idx], idx] if idx is not None else None)
            metrics.append([compact_distribution([rows[i][column] for i in indices]), *extrema])
        table.append([bucket_keys[0].isoformat(sep=' '), bucket_keys[-1].isoformat(sep=' '),
                      len(bucket_keys), len(indices), gap, metrics])
    return {'status': 'computed', 'column': time['name'], 'grain': grain, 'timezone': time.get('timezone'),
            'min': min(parsed.values()) if parsed else None, 'max': max(parsed.values()) if parsed else None,
            'null_count': null_count, 'invalid_count': invalid_count, 'valid_time_count': len(parsed),
            'observed_bucket_count': len(keys), 'observed_segment_count': len(segments),
            'represented_bucket_count': len(keys), 'all_valid_rows_represented': True,
            'window_columns': ['first_bucket', 'last_bucket', 'observed_buckets', 'row_count', 'spans_unobserved_gap', 'metrics'],
            'metric_cell': ['stats', 'min_evidence', 'max_evidence'],
            'evidence_cell': ['local_time', 'source_row_index_0_based'], 'windows': table}


def statistic_summary(rows, cube, max_windows=24):
    """全局统计 + 有限时间窗口，保留所有结果行的分布信息。

    用法：供默认 StatisticProfile 入口调用。原理：复用最多展开一个桶的全局统计，
    再从原行生成窗口。MySQL 可复用原全局聚合查询与 time_summary 的窗口分组。
    """
    if type(max_windows) is not int or max_windows <= 0:
        raise ValueError('max_windows 必须为正整数')
    base = statistic_profile_detail(rows, cube, max_time_buckets=1)
    # 无时间版验证不检查时间列，显式核对以避免把缺字段当成 NULL。
    if rows and cube.get('time') and cube['time']['name'] not in rows[0]:
        raise ValueError('缺少时间列')
    return {'schema_version': '3.0', 'profile_type': 'StatisticProfile', 'presentation': 'compact',
            'scope': {**base['scope'], 'time': cube.get('time')}, 'dataset': base['dataset'],
            'metric_schema': metric_schema(cube), 'stats_columns': STATS,
            'metric_stats': [compact_distribution([row[m['result_column']] for row in rows]) for m in cube.get('metrics', [])],
            'time': time_summary(rows, cube, max_windows),
            'semantics': 'all SQL result rows; descriptive values, no business reaggregation; valid_count unknown',
            'detail_command': 'python3 query_profile.py --kind time --offset 0 --limit 10'}


def dimension_set_summary(rows, names, cube, representative_limit):
    """所有组计算分布，少数组展开。返回全量频次/组间分布与候选组表。

    用法：names 为单维或二阶；representative_limit 限制具体组展开数。
    原理：按频次和各指标组中位数/标准差两端及中间轮转选取并去重；小样本
    不静默排除，保留行数和有限样本数。MySQL 先 GROUP BY，再对组统计作窗口排名。
    频次 Top-10 覆盖与代表组覆盖分别计算，不能互换；代表组不保证包含所有现象。
    """
    groups = group_rows(rows, names)
    ordered, _ = select_groups(groups, max(1, len(groups)))
    stats = {key: [compact_distribution([r[m['result_column']] for r in groups[key]]) for m in cube.get('metrics', [])]
             for key in ordered}
    count = len(rows)
    sizes = [len(group) for group in groups.values()]
    group_spreads, queues = [], [[(key, 'frequency') for key in ordered]]
    for i, metric in enumerate(cube.get('metrics', [])):
        entry = {'id': metric['id']}
        for col, label in ((6, 'median'), (9, 'stddev_pop')):
            valid = [key for key in ordered if stats[key][i][col] is not None]
            ranked = sorted(valid, key=lambda key: (stats[key][i][col], json.dumps(key, ensure_ascii=False)))
            entry[label] = spread([stats[key][i][col] for key in ranked])
            entry['groups_with_numeric_values'] = len(valid)
            if ranked:
                queues.append([(ranked[0], metric['id'] + ':' + label + ':low'),
                               (ranked[-1], metric['id'] + ':' + label + ':high'),
                               (ranked[len(ranked) // 2], metric['id'] + ':' + label + ':middle')])
        group_spreads.append(entry)
    chosen, reasons = [], defaultdict(list)
    if len(ordered) <= representative_limit:
        chosen = ordered
        for key in chosen:
            reasons[key].append('all_groups')
    else:
        # 轮转避免频次列表耗尽预算，令分布两端也有进入代表集的机会。
        while any(queues) and len(chosen) < representative_limit:
            for queue in queues:
                if queue and len(chosen) < representative_limit:
                    key, reason = queue.pop(0)
                    if key not in reasons:
                        chosen.append(key)
                    reasons[key].append(reason)
    shown = sum(len(groups[key]) for key in chosen)
    return {'dimensions': names, **dimension_overview(groups, count),
            'frequency': {'group_size_spread': spread(sizes),
                          'singleton_groups': sizes.count(1), 'singleton_rows': sizes.count(1),
                          'top10_row_count': sum(len(groups[key]) for key in ordered[:10]),
                          'top10_row_share': sum(len(groups[key]) for key in ordered[:10]) / count if count else None},
            'group_metric_spreads': group_spreads,
            'representatives': [[key, len(groups[key]), reasons[key], stats[key]] for key in chosen],
            'coverage': {'all_groups_used_for_summary': True, 'represented_group_count': len(chosen),
                         'represented_row_count': shown, 'remaining_group_count': len(groups) - len(chosen),
                         'remaining_row_count': count - shown, 'details_complete': len(chosen) == len(groups)}}


def dimension_summary(rows, cube, representative_limit=8, max_pairs=6):
    """生成单维和二阶的全量分布摘要；默认最多展开每项 8 个代表组。

    使用与 dimension_profile 类似；MySQL 同原维度 GROUP BY，下推全部组级统计，
    输出仅保留概况和候选行。全部组均被计算，摘要长度不随组数线性增长。
    """
    validate_input(rows, cube, representative_limit, max_pairs)
    names = sorted(d['name'] for d in cube.get('dimensions', []))
    singles = [dimension_set_summary(rows, [name], cube, representative_limit) for name in names]
    sizes = {s['dimensions'][0]: s['group_count'] for s in singles}
    pairs = sorted(combinations(names, 2), key=lambda p: (sizes[p[0]] * sizes[p[1]], p))
    return {'schema_version': '2.0', 'profile_type': 'DimensionProfile', 'presentation': 'compact',
            'status': 'computed' if names else 'not_applicable',
            'scope': {'population': 'sql_result_rows', 'result_row_count': len(rows),
                      'dimensions': cube.get('dimensions', []), 'time': cube.get('time'),
                      'filters_already_applied': True, 'source_completeness': 'unknown_from_result_only'},
            'metric_schema': metric_schema(cube), 'stats_columns': STATS,
            'spread_columns': ['min', 'p10', 'p50', 'p90', 'max'],
            'representative_columns': ['typed_dimension_values', 'row_count', 'selection_reasons', 'metric_stats'],
            'group_spread_population': 'each numeric-bearing group has equal weight, not SQL result rows',
            'single_dimensions': singles,
            'combinations': [dimension_set_summary(rows, list(pair), cube, representative_limit) for pair in pairs[:max_pairs]],
            'coverage': {'computed_pair_count': min(len(pairs), max_pairs), 'candidate_pair_count': len(pairs),
                         'skipped_pairs': [list(pair) for pair in pairs[max_pairs:]]},
            'detail_command': 'python3 query_profile.py --kind groups --dimensions channel --offset 0 --limit 10'}


def json_bytes(value):
    """测量实际紧凑 UTF-8 JSON 字节，不冒充精确 token 数。SQL 无对应计算。"""
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode())


def fit_budget(profile, max_bytes):
    """为单路输出设置硬字节上限，减少展开项但保持全量概况。

    使用：max_bytes 正整数，0 明确关闭限制；原地修改并返回。先减组合代表、
    再减少单维代表；时间窗口需在生成阶段减少以保持覆盖，不在此直接删除。
    最小摘要仍超限则报错，要求筛选指标或显式提高预算，不静默删除维度/指标。
    SQL 无对应逻辑，这是序列化输出控制。
    """
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError('max_bytes 必须为非负整数')
    profile['output_budget'] = {'max_bytes': max_bytes, 'measured_bytes': 0, 'reduced': False}
    items = profile.get('combinations', []) + profile.get('single_dimensions', [])
    while max_bytes and json_bytes(profile) + 16 > max_bytes:
        item = next((item for item in items if item['representatives']), None)
        if item is None:
            raise ValueError('最小摘要超过字节预算；请减少指标/维度或显式增大 --max-bytes')
        _, n, _, _ = item['representatives'].pop()
        coverage = item['coverage']
        coverage['represented_group_count'] -= 1
        coverage['represented_row_count'] -= n
        coverage['remaining_group_count'] += 1
        coverage['remaining_row_count'] += n
        coverage['details_complete'] = False
        profile['output_budget']['reduced'] = True
    for _ in range(4):
        profile['output_budget']['measured_bytes'] = json_bytes(profile)
    return profile


def bounded_statistic(rows, cube, max_windows=24, max_bytes=16000):
    """在预算不足时重分时间窗口，确保从未丢掉后面的时间。

    使用供默认入口调用；原理：逐次减半窗口，最少 1 个，仍超限交给 fit_budget 报错。
    MySQL 可用同一结果快照重设窗口标签；不需要重跑原业务查询。
    """
    current = max_windows
    while True:
        result = statistic_summary(rows, cube, current)
        if not max_bytes or json_bytes(result) + 128 <= max_bytes or current <= 1:
            result = fit_budget(result, max_bytes)
            result['output_budget']['reduced'] |= current < max_windows
            return fit_budget_preserve_measure(result)
        current = max(1, current // 2)


def fit_budget_preserve_measure(profile):
    """输出标记变化后更新字节计数，不改变摘要；SQL 无对应计算。"""
    for _ in range(4):
        profile['output_budget']['measured_bytes'] = json_bytes(profile)
    return profile
