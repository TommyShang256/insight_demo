"""按需查询 profile 明细：同一结果快照上筛选、分页，不执行原业务 SQL。"""
import argparse
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from dimension_profile import validate_input, dimension_key, group_rows, select_groups, describe_slice
from statistic_profile import parse_time, time_profile, distribution


def query_details(rows, cube, kind='groups', dimensions=None, filters=None, metrics=None,
                  start=None, end=None, offset=0, limit=10, max_bytes=16000):
    """显式查询值列表/时间桶/切片统计。

    使用：kind=groups/time/slice；filters 为 {name,type,value} 列表，各条件 AND；
    metrics 是指标 ID 列表；start/end 为左闭右开时间范围；offset 为同一快照的页偏移。
    原理：筛选原结果后稳定排序分页，只对本页的组生成完整统计。时间明细复用原桶算法，
    当前仍在内存计算所有匹配桶，再分页返回。字节超限时缩短本页并返回 next_offset，
    单个记录超限明确报错。SQL：在原结果快照 WHERE 后 GROUP BY；分页稳定排序。
    本地接口完整读取文件，不是百万行流式查询。快照 hash 用于检测跨页数据变化。
    """
    validate_input(rows, cube, 1, 0)
    if kind not in ('groups', 'time', 'slice'):
        raise ValueError('kind 必须为 groups/time/slice')
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('offset 必须非负，limit 必须为 1..100')
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError('max_bytes 必须非负，0 表示不设限制')
    dimension_names = {d['name'] for d in cube.get('dimensions', [])}
    dimensions = dimensions or []
    if kind == 'groups' and (not dimensions or len(set(dimensions)) != len(dimensions) or not set(dimensions) <= dimension_names):
        raise ValueError('groups 需要有效且不重复的普通维度列')
    spec_by_id = {m['id']: m for m in cube.get('metrics', [])}
    if metrics is not None and (len(set(metrics)) != len(metrics) or not set(metrics) <= spec_by_id.keys()):
        raise ValueError('指标 ID 无效或重复')
    selected_metrics = list(spec_by_id.values()) if metrics is None else [spec_by_id[m] for m in metrics]
    filters = filters or []
    if not isinstance(filters, list) or any(not isinstance(c, dict) for c in filters):
        raise ValueError('filters 必须为类型化条件对象列表')
    for condition in filters:
        if condition.get('name') not in dimension_names or condition.get('type') not in ('null', 'string', 'number', 'boolean'):
            raise ValueError('筛选条件需包含普通维度 name 和合法 type')
        value = condition.get('value')
        if value is not None and type(value) not in (str, int, float, bool):
            raise ValueError('筛选值必须为 JSON 标量')
        if type(value) is float and not math.isfinite(value):
            raise ValueError('筛选值不能为 NaN/Infinity')
        if dimension_key(value) != (condition['type'], value):
            raise ValueError('筛选条件的 type 与 value 不一致')
    time = cube.get('time')
    if (start is not None or end is not None or kind == 'time') and not time:
        raise ValueError('此查询需要时间维度')
    zone = ZoneInfo(time['timezone']) if time and time.get('timezone') else None
    lo = parse_time(start, zone) if start is not None else None
    hi = parse_time(end, zone) if end is not None else None
    if lo is not None and hi is not None and lo >= hi:
        raise ValueError('start 必须早于 end')
    selected = []
    for row in rows:
        if not all(dimension_key(row[c['name']]) == (c['type'], c.get('value')) for c in filters):
            continue
        if lo is not None or hi is not None:
            try:
                value = parse_time(row[time['name']], zone)
            except (ValueError, TypeError, OverflowError):
                continue
            if (lo is not None and value < lo) or (hi is not None and value >= hi):
                continue
        selected.append(row)
    selected_cube = {**cube, 'metrics': selected_metrics}
    extra = {}
    if kind == 'groups':
        groups = group_rows(selected, dimensions)
        keys, _ = select_groups(groups, max(1, len(groups)))
        total = len(keys)
        items = [describe_slice(groups[key], dimensions, key, selected_cube, len(rows)) for key in keys[offset:offset+limit]]
    elif kind == 'time':
        result = time_profile(selected, selected_cube, max(1, len(selected)))
        items = result.get('buckets', [])[offset:offset+limit]
        total = result.get('observed_bucket_count', 0)
        extra = {k: v for k, v in result.items() if k not in ('buckets', 'returned_bucket_count', 'bucket_limit', 'truncated')}
    else:
        items = [{'result_row_count': len(selected), 'metrics': {m['id']: distribution([r[m['result_column']] for r in selected]) for m in selected_metrics}}] if offset == 0 else []
        total = 1
    signature = json.dumps({'rows': rows, 'cube': cube}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    result = {'profile_type': 'ProfileDetail', 'kind': kind,
              'data_sha256': hashlib.sha256(signature.encode()).hexdigest(),
              'query': {'dimensions': dimensions, 'filters': filters, 'metrics': [m['id'] for m in selected_metrics], 'start': start, 'end': end},
              'scope': {'total_result_rows': len(rows), 'matched_result_rows': len(selected), 'row_share_denominator': 'total_result_rows'},
              'time_coverage': extra, 'offset': offset, 'total_count': total,
              'items': items, 'returned_count': 0, 'has_more': False, 'next_offset': None}
    while True:
        result.update(returned_count=len(items), has_more=offset+len(items)<total,
                      next_offset=offset+len(items) if offset+len(items)<total else None)
        size = len(json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode())
        if not max_bytes or size <= max_bytes:
            break
        if len(items) <= 1:
            raise ValueError('单条明细超过预算；减少 metrics 或显式增加 --max-bytes')
        items.pop()
    return result


def main():
    """CLI：python3 query_profile.py --kind groups --dimensions channel --limit 10。

    filters 使用 JSON 类型化条件数组，metrics 用空格分隔指标 ID；默认向 stdout 输出。
    参数转发 query_details，失败退出且不写错误的结果文件；无 SQL 执行。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=Path('mock/events.json'))
    parser.add_argument('--cube', type=Path, default=Path('mock/cube.json'))
    parser.add_argument('--kind', choices=['groups', 'time', 'slice'], default='groups')
    parser.add_argument('--dimensions', nargs='+')
    parser.add_argument('--metrics', nargs='+')
    parser.add_argument('--filters', default='[]')
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--max-bytes', type=int, default=16000)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = query_details(json.loads(args.data.read_text()), json.loads(args.cube.read_text()),
                           args.kind, args.dimensions, json.loads(args.filters), args.metrics,
                           args.start, args.end, args.offset, args.limit, args.max_bytes)
    text = json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n'
    if args.output:
        args.output.write_text(text)
    else:
        print(text, end='')


if __name__ == '__main__':
    main()
