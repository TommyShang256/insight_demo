# Cube Profiling：当前设计

StatisticProfile（statistic_profile.py）与 DimensionProfile（dimension_profile.py）均已实现 Python 标准库内存版本；subagent 调度尚未实现。运行方式与限制见 README.md。

## 输入边界

SQL 决定分析粒度和指标计算，profile 描述查询已经返回的数据。输入为 SQL 结果列表及 cube 元数据；不重新过滤、不反查底表、不生成新的业务汇总值。

普通维度和时间维度的 name 为结果字段名（英文小写，可含下划线），labelEn/labelCh 为中英文显示别名。每个维度包含 filters 数组，同维度 OR、跨维度 AND，空数组表示不限制。过滤已由原 SQL 执行，此规则不重复放入 cube 顶层。

指标通过 result_column 对应结果列，aggregation 保留原 SQL 聚合类型，仅用于说明；不影响分布算法。不要求指标公式、辅助列或额外计算声明。转化率结果可以独立被描述，不需要原分子、分母。

HAVING/LIMIT 等可能使返回结果只是部分组，profile 不把它们解释为源总体。scope 的 source_completeness 为 unknown_from_result_only。NULL、重复等质量指标均针对结果行，不能反推出源明细质量。

## StatisticProfile

- dataset：结果行数、字段观测类型、空值数与比例、完全重复结果的额外行数与重复组数。空表从 cube 获取声明字段名，但不猜测字段类型。
- metrics：非空、非数值、非有限值、零值与负值计数；对有限数值计算 min/max/mean/stddev_pop、P05/P25/P50/P75/P95。population=sql_result_rows，mean 仅为结果值的描述性均值。业务有效性规则未提供时 valid_count 保持 unavailable。
- time：结果时间覆盖、空值/无效值、观测桶数量、逐桶结果行数与各指标值分布。当前支持 hour/day/month。每桶中的 metrics.<id>.distribution 描述已有结果值，不生成合并后的业务指标。只列观测桶，不将未返回时间补为业务零值或标注为缺失。

示例：同一小时两个渠道分别返回转化率 1 和 1/9，profile 描述这两个值的极值、均值、分位数等，不把描述性均值声称为整体转化率。

## DimensionProfile

在相同结果边界内统计单维基数、空值、频次、结果行占比，以及单维/二阶组合切片内的指标分布。占比只采用结果行数，明确不计算指标贡献占比。

原子步骤：validate_input 校验输入；dimension_key 生成类型化键；group_rows 对全量结果分组；dimension_overview 统计基数与空值；select_groups 选出 Top-K；describe_slice 复用 distribution 并调用 time_coverage 描述时间范围。describe_dimension_set 组装单维或组合描述，dimension_profile 负责调度与输出。

默认 K=10，按结果行频次降序、类型化 JSON 键字典序稳定排序。仅对选中组计算指标分布与时间覆盖；剩余部分单独报告 remaining_group_count、remaining_row_count 和 remaining_row_share，不生成 Other 组。NULL 与字符串独立分组，参与分母；Top-K 不保证强制展示 NULL 组，但空值总量始终保留。

二阶组合默认最多 6 对，先按单维实际组数乘积（含 NULL）升序、再按维度名排序选择，因此默认不超过 4 维时覆盖全部对。显式记录跳过的组合；该顺序只用于成本控制。max_pairs=0 可禁用组合，不实现三阶及以上组合。

slice 保存带类型的维度名与值，供后续准确定位数据；不生成查询或分析建议。切片时间覆盖复用 StatisticProfile 的日期解析与时区规则，只记录观测范围、不同时间值数及有效/空/非法时间数量，不推断间隔缺失或业务零值。

## 实现与输出

两路独立输出：StatisticProfile 为 schema_version=2.0，包含 scope、dataset、metrics、time、calculation；DimensionProfile 为 schema_version=1.0，包含 status、scope、single_dimensions、combinations、coverage、calculation。最终两路合并尚未实现。

内存版本完整载入 JSON，精确排序计算分位数，保留唯一结果键统计重复。时间桶默认最多输出 1000 个，明确标记实际桶数与截断；可通过参数增加输出数量，不静默假定已完整返回。Python 浮点运算不保证十进制财务精度。

每个方法保留中文用途、使用方式、原理和边界说明，以及 MySQL 8.0+ 下推注释。下推应从完整原查询结果计算描述统计，保留原查询过滤和截断语义；需要时物化查询结果以复用同一快照。当前未接入真实 MySQL。

## Mock

python3 mock/generate.py 使用标准库 SQLite 执行 mock/query.sql，生成 mock/events.json 与 mock/cube.json。公式仅存在于生成结果的原查询中，profiling 不读取或执行 query.sql。

时间列 event_time 为小时粒度，字符串表示 Asia/Shanghai 本地时间。时间过滤为两个 OR 连接的左闭右开区间：2026-08-01 至 2026-08-08、2026-08-15 至 2026-08-22。中间一周在查询范围之外，不属于缺失数据。

生成器内部有 422 条源事件，过滤后 280 条，查询返回 279 行、279 个观测小时桶。结果列为时间、渠道、地区、商品，以及收入、事件数、转化率；不输出仅供后续计算的分子辅助列。

源数据中的变化、缺失、重复、零值、负值按 SQL 实际语义体现于结果。生成器验证原查询事件数守恒与分组键唯一；这些是 mock 的校验，不是 profile 对源数据的推断。
