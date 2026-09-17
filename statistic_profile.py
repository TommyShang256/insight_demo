"""SQL 结果集的确定性内存统计；Python 3.9+，无第三方依赖。

MySQL 下推约定（8.0+）：下方注释中的 r 均表示原 SQL 的完整结果：
WITH r AS (<原 SQL，保留 WHERE/HAVING/DISTINCT/LIMIT 等>) ...
生产中可先物化 r，避免多条统计查询重复执行或读到不同快照。
本模块只描述 SQL 返回值，不执行 SQL、不过滤输入、不生成新的业务汇总值。
参考：https://dev.mysql.com/doc/refman/8.0/en/window-function-descriptions.html
https://dev.mysql.com/doc/refman/8.4/en/aggregate-functions.html
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
import math
from pathlib import Path
import statistics
from zoneinfo import ZoneInfo


def finite_number(value):
    """判断一个值是否能作为有限数值参与统计。

    使用说明：传入任意单个值，返回 bool。例如 finite_number(12) 为 True，
    finite_number("12")、finite_number(True)、finite_number(None) 为 False。

    方法原理：只接受 Python 内置 int 和 float；整数直接接受，浮点数再用
    math.isfinite 排除 NaN 和正负无穷。使用精确类型判断，避免将 bool 当成整数。
    不做字符串转数值，也不判断业务有效性；负数仍是合法数值。
    """
    # SQL：数值类型由结果元数据确定；不要将字符串 CAST 后默认为有效数值。
    # MySQL 数值列没有本模块 Python API 所允许的 NaN/Infinity 样本。
    return type(value) in (int, float) and (type(value) is int or math.isfinite(value))


def unavailable(reason):
    """构造统一的“无法计算”结果，保留具体原因。

    使用说明：reason 为可读的原因字符串，例如 unavailable("未定义业务有效性规则")。
    返回包含 status="unavailable"、value=None 和 reason 的字典。

    方法原理：只封装状态，不执行计算。调用方据此区分“缺少条件而无法计算”
    与“已计算但值为 NULL”，后者应返回 status="computed"、value=None。
    """
    return {'status': 'unavailable', 'value': None, 'reason': reason}


def quantile(values, p):
    """计算一组已排序数值的指定分位数。

    使用说明：values 必须是升序排列、支持下标访问的有限数值序列；p 在 [0, 1]
    范围内，例如 quantile([0, 10, 20, 30], 0.25) 返回 7.5。
    返回数值；空序列返回 None，单元素序列返回该元素对应的数值。
    本方法不排序、不清洗样本、不校验 p；由调用方保证这些前提。

    方法原理：采用 Type 7 线性插值。对 n 个样本计算零基位置 h=(n-1)*p，
    取相邻位置 lo=floor(h)、hi=ceil(h)，返回
    values[lo] + (values[hi]-values[lo]) * (h-lo)。
    排序已由调用方完成，因此本步骤只需常数次下标访问。
    """
    # MySQL 8.0 下推，不使用不存在的 PERCENTILE_CONT：
    # WITH r AS (...), ranked AS (
    #   SELECT x, ROW_NUMBER() OVER(ORDER BY x) AS rn, COUNT(*) OVER() AS n
    #   FROM r WHERE x IS NOT NULL
    # ), positions AS (SELECT *, 1+(n-1)*:p AS h FROM ranked)
    # SELECT MAX(CASE WHEN rn=FLOOR(h) THEN x END)
    #   + (MAX(h)-FLOOR(MAX(h))) *
    #     (MAX(CASE WHEN rn=CEIL(h) THEN x END)
    #      -MAX(CASE WHEN rn=FLOOR(h) THEN x END)) FROM positions;
    # :p 由驱动参数绑定；原生 MySQL prepared statement 使用 ?。
    if not values:
        return None
    index = (len(values) - 1) * p
    lo, hi = math.floor(index), math.ceil(index)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def distribution(values):
    """描述一个指标在 SQL 结果行中的数值分布。

    使用说明：values 为可重复遍历且可取长度的列值序列，例如
    distribution([10, None, -2, 0])。返回计数、极值、均值、总体标准差和
    P05/P25/P50/P75/P95；字段含义由返回键给出。
    mean 是返回单元格的均值，不表示原始明细均值或整体业务指标。

    方法原理：保留有限数值并排序；None、非数值及非有限值分别计数。
    用 statistics 计算均值及总体标准差（方差分母为 n），用 quantile 插值。
    无数值样本时分布值为 None，单样本总体标准差为 0。
    负数参与计算；有限数值不等于业务有效样本，所以 valid_count 不作推断。
    排序通常耗时 O(n log n)，并保留 O(n) 个样本；本方法不是流式统计。
    """
    # SQL：COUNT(*), COUNT(x), COALESCE(SUM(x=0),0),
    # COALESCE(SUM(x<0),0), MIN(x), MAX(x), AVG(x), STDDEV_POP(x) FROM r。
    # COUNT(*)-COUNT(x) 为 NULL 数；分位数使用 quantile 注释中的窗口查询。
    samples = sorted(v for v in values if finite_number(v))
    nulls = sum(v is None for v in values)
    return {
        'population': 'sql_result_rows', 'sample_count': len(values),
        'non_null_count': len(values) - nulls, 'null_count': nulls,
        'finite_numeric_count': len(samples),
        'non_numeric_count': sum(v is not None and type(v) not in (int, float) for v in values),
        'non_finite_count': sum(type(v) is float and not math.isfinite(v) for v in values),
        'valid_count': unavailable('未提供可执行的业务有效性规则；数值有效样本见 finite_numeric_count'),
        'zero_count': samples.count(0), 'negative_count': sum(v < 0 for v in samples),
        'min': samples[0] if samples else None, 'max': samples[-1] if samples else None,
        'mean': statistics.mean(samples) if samples else None,
        'stddev_pop': statistics.pstdev(samples) if samples else None,
        'quantile_method': 'linear_type_7',
        'quantiles': {f'p{p:02d}': quantile(samples, p / 100) for p in (5, 25, 50, 75, 95)},
    }


def field_profile(values):
    """描述一个结果字段的观测类型及空值情况。

    使用说明：传入整列值序列，例如 field_profile([1, None, 2]) 返回
    observed_types=["int"]、null_count=1、null_ratio=1/3 等信息。
    values 需要支持重复遍历和 len，不直接接收一次性生成器。

    方法原理：遍历非 None 值，收集 Python 类型名并排序；以 None 数量除以
    总行数计算空值比例。空序列的比例为 None，全空列的观测类型列表为空。
    空字符串、NaN 不计为 NULL；观测类型不等同于数据库声明类型。
    """
    # SQL：COUNT(*)-COUNT(col) 与其 / NULLIF(COUNT(*),0)。
    # 类型读取驱动的结果列元数据；表达式结果不要查源表 INFORMATION_SCHEMA 猜类型。
    # 内存版本只报告观测类型；空表/全 NULL 无法还原声明类型。
    types = {type(v).__name__ for v in values if v is not None}
    nulls = sum(v is None for v in values)
    return {'type_source': 'observed_python_values', 'observed_types': sorted(types),
            'null_count': nulls, 'null_ratio': nulls / len(values) if values else None}


def duplicate_profile(rows, columns):
    """计算重复结果组数，以及重复组中多出来的行数。

    使用说明：rows 为结果行字典列表，columns 为固定顺序的全部结果列名。
    例如 duplicate_profile([{"x": 1}, {"x": 1}, {"x": 2}], ["x"])
    返回 extra_row_count=1、duplicate_group_count=1。
    如只传部分列，统计将变为该列组合的重复；完整结果重复必须传入所有列。

    方法原理：将每一列的值转为可哈希键，再把整行组合成元组，用 Counter
    统计出现次数。出现 n 次的组贡献 n-1 条额外行；n>1 的组贡献一个重复组。
    字符串区分大小写和尾空格；None 参与分组；数值 1 和 1.0 视为相同。
    仅识别 SQL 返回结果的重复，不能还原聚合前的源数据重复。
    需要保存唯一结果键，额外内存随唯一行数及列数增长。
    """
    # SQL：SELECT COALESCE(SUM(n-1),0) AS extra_rows,
    # COALESCE(SUM(n>1),0) AS duplicate_groups FROM
    # (SELECT COUNT(*) n FROM r GROUP BY <所有结果列>) g。
    # 文本列需使用二进制比较（含尾空格），保持与 Python 大小写敏感的相等语义一致。
    # 不用 COUNT(DISTINCT 多列)：NULL 处理会漏组。只描述结果重复，不描述源重复。
    def key(value):
        """将单个结果值转换为重复检测所用的可哈希比较键。

        使用说明：仅供 duplicate_profile 内部调用，value 必须为受支持的标量。
        key(1) 与 key(1.0) 生成相等的键；key(True) 与 key(1) 不相等。

        方法原理：普通值由“类型标签、原值”组成，int/float 共用 number 标签。
        NaN 和无穷值改用字符串标签，使多个 NaN 可以稳定归入同一重复组。
        不接受列表、字典等不可哈希值；入口函数会提前拒绝这些输入。
        """
        if type(value) is float and not math.isfinite(value):
            return ('non_finite', str(value))
        return ('number' if type(value) in (int, float) else type(value).__name__, value)
    counts = Counter(tuple(key(row[c]) for c in columns) for row in rows)
    return {'extra_row_count': sum(n - 1 for n in counts.values()),
            'duplicate_group_count': sum(n > 1 for n in counts.values()),
            'comparison': 'all_result_columns_case_sensitive'}


def parse_time(value, timezone):
    """把 ISO 日期或时间字符串转换为用于比较、分桶的本地 datetime。

    使用说明：value 为 ISO 字符串；timezone 为 ZoneInfo 等时区对象或 None。
    例如 parse_time("2026-08-01T01:00:00Z", ZoneInfo("Asia/Shanghai"))
    返回不带 tzinfo 的 datetime(2026, 8, 1, 9, 0)。
    非字符串、格式错误或有偏移量却未指定目标时区时抛出 ValueError。

    方法原理：先将 Z 换为 +00:00，再调用 datetime.fromisoformat。
    有时区的值转换到目标时区后移除 tzinfo；没有时区的值按已有本地时间保留，
    不进行时区平移。纯日期按当天 00:00:00 处理。
    输出表达本地钟表时间；不区分夏令时回拨时重复出现的两个本地小时。
    """
    # SQL：已声明 DATETIME/TIMESTAMP 直接统计；字符串列需按声明格式 STR_TO_DATE。
    # TIMESTAMP 设置会话 time_zone；DATETIME 必须由上游明确所属时区。
    # 本实现接收 ISO 日期/时间；有 offset 的值转换到 cube 时区。
    if not isinstance(value, str):
        raise ValueError('时间值必须是 ISO 日期或时间字符串')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is not None:
        if timezone is None:
            raise ValueError('带 offset 的时间需要 cube.time.timezone')
        parsed = parsed.astimezone(timezone).replace(tzinfo=None)
    return parsed


def time_profile(rows, cube, max_buckets):
    """统计时间覆盖，并描述实际出现的时间桶内的指标值分布。

    使用说明：rows/cube 为已校验的结果和元数据；
    max_buckets 为正整数，限制按时间升序返回的桶数。
    例如 time_profile(rows, cube, 1000)。
    返回覆盖范围、空值/无效值数量、桶列表及截断状态；没有时间维度返回
    not_applicable，不支持的粒度返回 unavailable。

    方法原理：按 cube.time 的 name/timezone 解析时间，以 hour/day/month
    格式生成桶键。保留桶内各结果行，调用 distribution 描述每个指标列的分布。
    覆盖范围和实际桶数使用全部有效时间；输出桶数受 max_buckets 限制，超限
    时 truncated=True。空值与解析失败分别计数，不参加覆盖和分桶。

    边界：不重新执行 filters，不补齐空桶，不将过滤区间之外的时间视为缺失。
    调用方应保证 grain 与查询的时间粒度一致，本方法不从 SQL 推断或校验粒度。
    错误的时区名称会抛出 ZoneInfoNotFoundError；日期值解析错误则计入 invalid_count。
    需保存时间值和各桶行引用，限制输出桶数并不会限制输入的内存占用。
    """
    # SQL：MIN(t),MAX(t),COUNT(*)-COUNT(t)；小时桶
    # DATE_FORMAT(t,'%Y-%m-%d %H:00:00')，日桶 DATE(t)，月桶 DATE_FORMAT(t,'%Y-%m-01')。
    # 按桶 GROUP BY，计算 COUNT/MIN/MAX/AVG/STDDEV_POP 等描述统计；
    # 分位数窗口按桶 PARTITION BY，沿用 quantile 的位置插值，不重算业务指标。
    # 仅输出观测桶，不用递归 CTE 跨 MIN/MAX 补零：过滤区间中间可能存在排除范围。
    time = cube.get('time')
    if not time:
        return {'status': 'not_applicable', 'reason': 'cube 无时间维度'}
    column = time['name']
    zone = ZoneInfo(time['timezone']) if time.get('timezone') else None
    groups = defaultdict(list)
    parsed_values = []
    invalid_count = 0
    grain = time.get('grain')
    formats = {'hour': '%Y-%m-%d %H:00:00', 'day': '%Y-%m-%d', 'month': '%Y-%m-01'}
    if grain not in formats:
        return {'status': 'unavailable', 'reason': '当前时间序列支持显式 hour/day/month 粒度'}
    for row in rows:
        if row[column] is None:
            continue
        try:
            value = parse_time(row[column], zone)
        except (ValueError, TypeError, OverflowError):
            invalid_count += 1
            continue
        parsed_values.append(value)
        groups[value.strftime(formats[grain])].append(row)
    buckets = []
    for label in sorted(groups)[:max_buckets]:
        group = groups[label]
        buckets.append({'time': label, 'result_row_count': len(group), 'metrics': {
            m['id']: {'distribution': distribution([row[m['result_column']] for row in group])}
            for m in cube.get('metrics', [])}})
    return {
        'status': 'computed', 'column': column, 'grain': grain, 'timezone': time.get('timezone'),
        'min': min(parsed_values).isoformat(sep=' ') if parsed_values else None,
        'max': max(parsed_values).isoformat(sep=' ') if parsed_values else None,
        'null_count': sum(r[column] is None for r in rows), 'invalid_count': invalid_count,
        'valid_time_count': len(parsed_values), 'observed_bucket_count': len(groups),
        'returned_bucket_count': len(buckets), 'truncated': len(groups) > max_buckets,
        'bucket_limit': max_buckets, 'buckets': buckets,
        'gap_interpretation': '仅列出观测桶；未返回时间不推断为缺失或业务零值',
    }


def statistic_profile_detail(rows, cube, max_time_buckets=1000):
    """从一个 cube 的 SQL 结果生成完整 StatisticProfile，是主要调用入口。

    使用说明：rows 为 list[dict]，每行列集合相同，只包含 JSON 标量；
    cube 提供 dimensions、metrics 和可选 time；max_time_buckets 默认为 1000。
    调用 profile = statistic_profile_detail(rows, cube)，返回可序列化的字典，包含
    scope、dataset、metrics、time 和 calculation，不修改传入的行或元数据。
    缺少必需列、列不一致、重复指标 ID 等已检查的错误会抛出 ValueError；
    缺少元数据必需键仍可能抛出 KeyError，调用方应遵守 cube 的字段约定。

    方法原理：先验证输入与结果列，再分别调用字段质量、重复检测、指标分布、
    以及时间统计方法，最后组装输出。空结果时从 cube 声明推导字段名，
    不推断字段类型。aggregation 仅保留原查询的聚合类型，不参与计算分派。

    边界：以传入结果为统计总体，不执行过滤或 SQL，不推断查询是否被 LIMIT
    截断，也不推断明细质量。指标直接按结果值描述，不依赖公式或辅助列。
    本版本完整持有输入并为各统计创建中间集合，未实现流式或数据库执行。
    """
    # SQL 下推入口：先检查结果列元数据，再在同一份 r 上运行上述统计。
    # 绝不拼接/执行 cube 内 SQL 字符串；本版本只把它们保留为说明。
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError('rows 必须是对象列表')
    if not isinstance(cube, dict) or type(max_time_buckets) is not int or max_time_buckets <= 0:
        raise ValueError('cube 必须是对象，max_time_buckets 必须为正整数')
    dimensions = cube.get('dimensions', [])
    metrics = cube.get('metrics', [])
    required = [d['name'] for d in dimensions] + [m['result_column'] for m in metrics]
    if cube.get('time'):
        required.append(cube['time']['name'])
    if len({m['id'] for m in metrics}) != len(metrics):
        raise ValueError('指标 id 不得重复')
    columns = sorted(rows[0]) if rows else sorted(set(required))
    if rows and not set(required) <= set(columns):
        raise ValueError(f'缺少 cube 声明的结果列：{sorted(set(required)-set(columns))}')
    for row in rows:
        if set(row) != set(columns):
            raise ValueError('每行必须包含相同结果列；缺字段不等于 SQL NULL')
        if any(v is not None and type(v) not in (str, int, float, bool) for v in row.values()):
            raise ValueError('结果值只支持 JSON 标量；日期请传 ISO 字符串')
    return {
        'schema_version': '2.0', 'profile_type': 'StatisticProfile',
        'scope': {'population': 'sql_result_rows', 'filters_already_applied': True,
                  'filter_combination': 'OR_within_dimension_AND_between_dimensions',
                  'dimensions': dimensions, 'time': cube.get('time'),
                  'source_completeness': 'unknown_from_result_only'},
        'dataset': {'result_row_count': len(rows), 'field_count': len(columns),
                    'fields': {c: field_profile([r[c] for r in rows]) for c in columns},
                    'duplicates': duplicate_profile(rows, columns)},
        'metrics': {m['id']: {
            'column': m['result_column'], 'aggregation': m.get('aggregation'),
            'distribution': distribution([r[m['result_column']] for r in rows]),
        } for m in metrics},
        'time': time_profile(rows, cube, max_time_buckets),
        'calculation': {'backend': 'memory', 'sampling': False,
                        'quantiles': 'exact_order_statistics_with_linear_interpolation',
                        'numeric_precision': 'Python integer / floating-point; not decimal accounting'},
    }


def statistic_profile(rows, cube, max_windows=24, max_bytes=16000):
    """默认返回紧凑摘要；全量计算，最多 24 个时间展示窗口。

    使用：statistic_profile(rows, cube)，字节上限默认 16000；0 为显式不设上限。
    原理：复用原全局统计，并从原结果值压缩时间窗口，预算不足时减少窗口数。
    完整旧结构可用 statistic_profile_detail；按切片分页使用 query_profile.py。
    MySQL 下推参见 compact_profile.time_summary，各窗口不生成业务总量。
    """
    from compact_profile import bounded_statistic
    return bounded_statistic(rows, cube, max_windows, max_bytes)


def main():
    """从命令行读取 cube 与结果 JSON，计算并写出 StatisticProfile 文件。

    使用说明：在项目目录执行 python3 statistic_profile.py；默认读取
    mock/cube.json、mock/events.json，写入 mock/statistic_profile.json。
    默认输出紧凑摘要；--max-windows 控制展示窗口，--max-bytes 控制字节预算。
    --detail 显式使用详细结构，此时 --max-time-buckets 指定桶数量上限。
    可用 --cube、--data、--output 指定路径。
    路径相对于当前工作目录；输出父目录需已存在，同名文件会被覆盖。

    方法原理：argparse 解析参数，json.loads 读取对象，调用 statistic_profile，
    再以中文可读、缩进格式写出 JSON。allow_nan=False 防止输出非标准数值。
    本方法无业务返回值，只写文件并打印摘要；读取、解析、校验和写入异常会
    直接向调用环境抛出，不启动数据库连接，也不执行 cube 中的 SQL。
    """
    # 此 CLI 只读取查询结果文件；SQL 下推版本应由数据库驱动取得 r 的快照。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cube', type=Path, default=Path('mock/cube.json'))
    parser.add_argument('--data', type=Path, default=Path('mock/events.json'))
    parser.add_argument('--output', type=Path, default=Path('mock/statistic_profile.json'))
    parser.add_argument('--max-time-buckets', type=int, default=1000)
    parser.add_argument('--detail', action='store_true', help='显式输出旧版详细结构')
    parser.add_argument('--max-windows', type=int, default=24)
    parser.add_argument('--max-bytes', type=int, default=16000)
    args = parser.parse_args()
    rows, cube = json.loads(args.data.read_text()), json.loads(args.cube.read_text())
    profile = (statistic_profile_detail(rows, cube, args.max_time_buckets) if args.detail
               else statistic_profile(rows, cube, args.max_windows, args.max_bytes))
    args.output.write_text(json.dumps(profile, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n')
    print(f"Wrote {args.output}: {profile['dataset']['result_row_count']} result rows")


if __name__ == '__main__':
    main()
