"""描述 SQL 结果的维度分布；不重算业务指标，使用 Python 标准库。

MySQL 8.0+ 注释中的 r 始终是完整原查询的结果或同一份物化快照。
字符串分组需采用区分大小写及尾空格的二进制比较，不能直接依赖默认排序规则。
"""
import argparse
from collections import defaultdict
from itertools import combinations
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from statistic_profile import distribution, parse_time


def validate_input(rows, cube, top_k, max_pairs):
    """校验维度统计输入，避免缺列、重名或非法值导致静默错误。

    使用：validate_input(rows, cube, 10, 6)，成功无返回值，错误抛 ValueError。
    rows 是列集合一致的字典列表，cube 包含可选 dimensions/metrics/time；
    top_k 为正整数，max_pairs 为非负整数，0 表示不计算二阶组合。
    原理：检查配置及必需列，再遍历所有输入值验证 JSON 标量约束。
    非有限指标值由 distribution 描述；维度中的非有限数值无法形成可靠切片，
    会被拒绝。空结果允许按 cube 声明描述零组，不猜测实际数据库类型。
    """
    # SQL 下推：执行前从驱动结果元数据核对列名；只绑定值，不直接拼接外部标识符。
    if not isinstance(cube, dict) or not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError('cube 必须是对象，rows 必须是对象列表')
    if type(top_k) is not int or top_k <= 0 or type(max_pairs) is not int or max_pairs < 0:
        raise ValueError('top_k 必须为正整数，max_pairs 必须为非负整数')
    dimensions, metrics = cube.get('dimensions', []), cube.get('metrics', [])
    if not isinstance(dimensions, list) or not isinstance(metrics, list):
        raise ValueError('dimensions 和 metrics 必须为列表')
    names = []
    for d in dimensions:
        if not isinstance(d, dict) or not isinstance(d.get('name'), str) or not d['name']:
            raise ValueError('维度必须包含非空字符串 name')
        names.append(d['name'])
    if len(set(names)) != len(names):
        raise ValueError('维度 name 不得重复')
    ids, required = [], list(names)
    for m in metrics:
        if not isinstance(m, dict) or any(not isinstance(m.get(k), str) or not m[k] for k in ('id', 'result_column')):
            raise ValueError('指标必须包含非空字符串 id 和 result_column')
        ids.append(m['id'])
        required.append(m['result_column'])
    if len(set(ids)) != len(ids):
        raise ValueError('指标 id 不得重复')
    time = cube.get('time')
    if time:
        if not isinstance(time, dict) or not isinstance(time.get('name'), str) or not time['name']:
            raise ValueError('时间维度必须包含 name')
        required.append(time['name'])
        if time.get('timezone'):
            ZoneInfo(time['timezone'])
    columns = set(rows[0]) if rows else set(required)
    if not set(required) <= columns:
        raise ValueError(f'缺少结果列：{sorted(set(required) - columns)}')
    for row in rows:
        if set(row) != columns:
            raise ValueError('每行必须包含相同结果列；缺字段不等于 NULL')
        if any(v is not None and type(v) not in (str, int, float, bool) for v in row.values()):
            raise ValueError('结果值仅支持 JSON 标量')
        if any(type(row[n]) is float and not math.isfinite(row[n]) for n in names):
            raise ValueError('维度值不支持 NaN 或 Infinity')


def dimension_key(value):
    """把维度标量编码为稳定、可哈希且不混淆类型的分组键。

    使用：dimension_key(None) 返回 ('null', None)，字符串 "null" 属于 string。
    原理：给标量附类型标签；整数与整数值浮点数合并，bool 与数字分开。
    数字 1 和 1.0 均规范为整数 1，使输入行顺序不影响输出；不转换字符串。
    调用前需验证值为有限 JSON 标量；字符串比较区分大小写及尾空格。
    """
    # SQL：NULL 自成组；类型由结果列定义。混合类型 JSON 列需额外按 JSON_TYPE 分组。
    if value is None:
        return ('null', None)
    if type(value) is bool:
        return ('boolean', value)
    if type(value) in (int, float):
        return ('number', int(value) if type(value) is int or value.is_integer() else value)
    return ('string', value)


def group_rows(rows, dimension_names):
    """按一个或多个维度值建立分组，保留结果行的引用。

    使用：group_rows(rows, ['channel']) 或 ['channel', 'region']；返回键到行列表的字典。
    原理：逐行取维度值，调用 dimension_key 生成元组键后追加到对应组。
    NULL 参与分组，不丢弃任何结果行；只划分切片，不执行指标汇总。
    时间约 O(n*d)，额外内存包含 n 个行引用及不同维度组合的键。
    """
    # SQL：SELECT d1,d2,COUNT(*) FROM r GROUP BY d1,d2；切片取数使用 NULL 安全比较 <=>。
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(dimension_key(row[name]) for name in dimension_names)].append(row)
    return groups


def dimension_overview(groups, total_rows):
    """描述分组基数以及含空维度值的结果行比例。

    使用：传入 group_rows 的结果和总结果行数；返回 group_count、
    non_null_group_count、null_row_count、null_row_share。
    原理：遍历分组键，任何组成维度为 NULL 即计入含空行；每行只计一次。
    单维的 non_null_group_count 就是非空基数；组合时表示所有分量非空的组合数。
    分母包含空值行。零行时比例为 None，而计数为 0。
    """
    # SQL：对 GROUP BY 结果 COUNT(*) 得组数；不要用多列 COUNT(DISTINCT ...) 计算含 NULL 的组数。
    # 含空行数 COALESCE(SUM(d1 IS NULL OR d2 IS NULL),0)，除以 NULLIF(COUNT(*),0)。
    null_rows = sum(len(rows) for key, rows in groups.items() if any(kind == 'null' for kind, _ in key))
    return {'group_count': len(groups),
            'non_null_group_count': sum(all(kind != 'null' for kind, _ in key) for key in groups),
            'null_row_count': null_rows,
            'null_row_share': null_rows / total_rows if total_rows else None}


def select_groups(groups, top_k):
    """按频次选展示组，同时报告未展示部分的准确覆盖量。

    使用：select_groups(groups, 10) 返回 (选中键列表, 覆盖字典)。
    原理：组大小降序，平局按类型化键的紧凑 JSON 文本字典序排列，保证可复现。
    排序是成本/展示规则，不表示洞察价值；不额外强制保留 NULL 组。
    剩余组只报告数量和结果行数，不生成容易与真实值混淆的 Other 组。
    """
    # SQL：先 GROUP BY 计数，再 ROW_NUMBER() OVER(ORDER BY n DESC, <明确的类型/二进制键>)。
    # 对 rn<=K 与 rn>K 分别统计覆盖。Python JSON 键排序需在 SQL 中显式匹配，不能用默认 collation。
    ordered = sorted(groups, key=lambda key: (-len(groups[key]), json.dumps(key, ensure_ascii=False, separators=(',', ':'))))
    selected = ordered[:top_k]
    shown = sum(len(groups[key]) for key in selected)
    total = sum(len(rows) for rows in groups.values())
    return selected, {'top_k': top_k, 'selected_group_count': len(selected),
                      'selected_row_count': shown,
                      'selected_row_share': shown / total if total else None,
                      'remaining_group_count': len(ordered) - len(selected),
                      'remaining_row_count': total - shown,
                      'remaining_row_share': (total - shown) / total if total else None,
                      'truncated': len(ordered) > top_k,
                      'ordering': 'row_count_desc_then_typed_json_key_asc'}


def time_coverage(rows, time):
    """描述一个切片的时间覆盖，不展开时间桶或推断连续性。

    使用：time_coverage(slice_rows, cube.get('time'))，无时间定义返回 not_applicable。
    原理：复用 parse_time 的时区规则，遍历后统计极值、有效/空/非法时间数及
    不同本地时间值数量。不同时间值数不是持续时长，不据此把间隔认定为缺失。
    """
    # SQL：MIN(t),MAX(t),COUNT(t),COUNT(DISTINCT t),COUNT(*)-COUNT(t)，按切片分组。
    # 字符串时间先按声明格式解析；设置会话时区，不能使用字符串 MIN/MAX 代替时间比较。
    if not time:
        return {'status': 'not_applicable'}
    zone = ZoneInfo(time['timezone']) if time.get('timezone') else None
    values, null_count, invalid_count = [], 0, 0
    for row in rows:
        value = row[time['name']]
        if value is None:
            null_count += 1
            continue
        try:
            values.append(parse_time(value, zone))
        except (ValueError, TypeError, OverflowError):
            invalid_count += 1
    return {'status': 'computed', 'timezone': time.get('timezone'),
            'min': min(values).isoformat(sep=' ') if values else None,
            'max': max(values).isoformat(sep=' ') if values else None,
            'valid_time_count': len(values), 'distinct_time_count': len(set(values)),
            'null_count': null_count, 'invalid_count': invalid_count}


def describe_slice(rows, dimension_names, key, cube, total_rows):
    """描述一个已选切片的定位条件、结果行占比、指标分布与时间覆盖。

    使用：rows 为当前组，key 来自 group_rows，total_rows 为整个 SQL 结果行数。
    返回 slice、result_row_count、result_row_share、metrics、time。
    原理：行数除以全局总行数；指标逐列复用 distribution。slice 保存类型化值，
    消费方可以准确定位 NULL/字符串/数字；不拼接执行 SQL，不生成业务贡献占比。
    指标均值仍只描述该切片已有结果值，不是合成的整体业务指标。
    """
    # SQL：WHERE d1 <=> ? AND d2 <=> ? 后计算描述统计；占比分母取未切片 r 的 COUNT(*)。
    # 批量下推用 GROUP BY；分位数 ROW_NUMBER/COUNT 按维度 PARTITION BY 再线性插值。
    return {'slice': [{'name': name, 'type': kind, 'value': value}
                      for name, (kind, value) in zip(dimension_names, key)],
            'result_row_count': len(rows),
            'result_row_share': len(rows) / total_rows if total_rows else None,
            'metrics': {m['id']: {'column': m['result_column'], 'aggregation': m.get('aggregation'),
                                 'distribution': distribution([row[m['result_column']] for row in rows])}
                        for m in cube.get('metrics', [])},
            'time': time_coverage(rows, cube.get('time'))}


def describe_dimension_set(rows, names, cube, top_k):
    """组合原子步骤，生成单维或二阶维度的完整描述。

    使用：describe_dimension_set(rows, ['channel'], cube, 10)。返回概况、展示覆盖、
    切片列表。原理：先对全部输入分组计数，再只为选中组计算指标分布和时间覆盖。
    截断影响详细切片输出，不改变基数、空值计数、分母及剩余覆盖量。
    """
    # SQL：先物化 GROUP BY 频次表，选出 Top-K 键，再以 <=> 回连 r 计算切片分布。
    groups = group_rows(rows, names)
    selected, coverage = select_groups(groups, top_k)
    return {'dimensions': names, 'status': 'computed',
            **dimension_overview(groups, len(rows)), 'coverage': coverage,
            'groups': [describe_slice(groups[key], names, key, cube, len(rows)) for key in selected]}


def dimension_profile(rows, cube, top_k=10, max_pairs=6):
    """生成 DimensionProfile，是对外的内存计算入口。

    使用：dimension_profile(rows, cube)，默认每组维度展示 10 个值、最多计算 6 个
    二阶维度对；max_pairs=0 禁用组合。无普通维度返回 not_applicable，空结果保留零计数。
    原理：先描述所有单维，按各对单维实际组数乘积升序、维度名升序挑选组合，
    再逐对计算。所有未执行组合及原因显式返回。选对仅用于限制成本，不代表业务价值。
    全量输入留在内存中，每个维度组合顺序处理；未实现三阶组合或流式计算。
    """
    # SQL 下推：逐个维度/选中维度对运行上述 GROUP BY 描述；共用原查询 r 的快照。
    validate_input(rows, cube, top_k, max_pairs)
    names = sorted(d['name'] for d in cube.get('dimensions', []))
    singles = [describe_dimension_set(rows, [name], cube, top_k) for name in names]
    sizes = {item['dimensions'][0]: item['group_count'] for item in singles}
    pairs = sorted(combinations(names, 2), key=lambda pair: (sizes[pair[0]] * sizes[pair[1]], pair))
    selected = pairs[:max_pairs]
    return {'schema_version': '1.0', 'profile_type': 'DimensionProfile',
            'status': 'computed' if names else 'not_applicable',
            'scope': {'population': 'sql_result_rows', 'result_row_count': len(rows),
                      'filters_already_applied': True, 'dimensions': cube.get('dimensions', []),
                      'time': cube.get('time'), 'source_completeness': 'unknown_from_result_only'},
            'single_dimensions': singles,
            'combinations': [describe_dimension_set(rows, list(pair), cube, top_k) for pair in selected],
            'coverage': {'candidate_pair_count': len(pairs), 'computed_pair_count': len(selected),
                         'max_pairs': max_pairs, 'pair_selection': 'group_count_product_asc_then_names_asc',
                         'skipped_pairs': [{'dimensions': list(pair), 'reason': 'max_pairs_limit'} for pair in pairs[max_pairs:]]},
            'calculation': {'backend': 'memory', 'sampling': False,
                            'row_share_denominator': 'all_sql_result_rows_including_null_dimensions'}}


def main():
    """从文件生成维度统计 JSON。

    使用：python3 dimension_profile.py；可传 --cube、--data、--output、--top-k、
    --max-pairs。默认读取 mock 输入，输出 mock/dimension_profile.json。
    原理：读取 JSON 后调用 dimension_profile 并写文件，不连接数据库；现有输出会覆盖。
    路径相对工作目录，父目录必须存在；错误直接抛出，非标准 JSON 数值不会写入输出。
    """
    # SQL 版本由数据库驱动提供原查询结果快照；CLI 本身无可下推业务计算。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cube', type=Path, default=Path('mock/cube.json'))
    parser.add_argument('--data', type=Path, default=Path('mock/events.json'))
    parser.add_argument('--output', type=Path, default=Path('mock/dimension_profile.json'))
    parser.add_argument('--top-k', type=int, default=10)
    parser.add_argument('--max-pairs', type=int, default=6)
    args = parser.parse_args()
    profile = dimension_profile(json.loads(args.data.read_text()), json.loads(args.cube.read_text()), args.top_k, args.max_pairs)
    args.output.write_text(json.dumps(profile, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(f"Wrote {args.output}: {len(profile['single_dimensions'])} dimensions, {len(profile['combinations'])} pairs")


if __name__ == '__main__':
    main()
