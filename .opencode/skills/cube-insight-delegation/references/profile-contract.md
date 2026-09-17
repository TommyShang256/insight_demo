# 紧凑 Profile 字段契约

默认 StatisticProfile 3.0 / DimensionProfile 2.0，presentation=compact。先按列定义解码数组；不可套用旧版本的嵌套路径。旧详细函数保留 StatisticProfile 2.0 / DimensionProfile 1.0，仅显式使用。

## 共享规则

- metric_schema 定义指标顺序、列名与原聚合类型；stats_columns 定义每个数值数组的顺序。
- 当前 stats_columns：finite_numeric_count、null_count、non_numeric_count、non_finite_count、min、p05、p50、p95、max、stddev_pop。样本总量由所在窗口/代表组的 row_count 或全局行数提供。
- 不要求业务公式、分子、分母、业务汇总值。未声明业务有效性规则，有限数值量不等于业务有效样本量。
- output_budget 给 max_bytes、measured_bytes、reduced。字节按紧凑 UTF-8 JSON 实测，不含文件末尾换行，不是模型精确 token 数。默认每路最多 16000 字节。
- scope 保留过滤、维度、时间定义；过滤已执行。source_completeness=unknown_from_result_only，不能声称源总体完整。

## StatisticProfile 3.0

- dataset：result_row_count、字段类型/空值、完全重复结果统计，与旧版口径一致。
- metric_stats：按 metric_schema 顺序，给出全体结果行的指标分布。
- time：状态、粒度、时区、实际时间范围、NULL/无效/有效时间数、observed_bucket_count 和 represented_bucket_count。
- time.windows：按 window_columns 解码：first_bucket、last_bucket、observed_buckets、row_count、spans_unobserved_gap、metrics。
- 窗口 metrics 按指标顺序，每个元素为 [stats,min_evidence,max_evidence]。证据为 [local_time,source_row_index_0_based]；索引指向原结果数组，取数后可查完整维度键。
- 默认最多 24 个展示窗口，所有有效时间行均参与，不是仅截取前 24 小时。优先不跨未观测间隔；连续段过多或预算收紧时可以跨段打包，但 spans_unobserved_gap=true。窗口首尾之间不保证连续，不推断间隔缺失原因。
- 窗口分位数从原结果值计算，不平均小时桶分位数。多个系列混合时这是混合值分布，仍须读具体系列才能说明它的走势。

## DimensionProfile 2.0

- single_dimensions / combinations：每项含 dimensions、group_count、non_null_group_count、null_row_count/null_row_share。
- frequency.group_size_spread：全部组大小的 [min,P10,P50,P90,max]；singleton_groups/singleton_rows；top10_row_count/top10_row_share。
- group_metric_spreads：各指标在全部含数值的组上的中位数和总体标准差分布，均用 spread_columns=[min,p10,p50,p90,max]。每组等权，groups_with_numeric_values 给参与组数。不能与全局结果行分布混同。
- representatives：按 representative_columns=[typed_dimension_values,row_count,selection_reasons,metric_stats] 解码。typed_dimension_values 按 dimensions 顺序为 [type,value]，类型区分 null/string/number/boolean。
- 组数少于展开上限时完整展示；高基数时轮转选择高频和各指标组中位数/离散程度两端及中间组，去重后限量。它不是仅按频次排序的 Top-K，也不保证覆盖所有重要现象。
- coverage.all_groups_used_for_summary=true 表示所有组参与摘要；represented_* 是明确展开的组/行，remaining_* 是未展开部分，不代表这些组被排除出统计。
- coverage.details_complete 为是否列全所有组。output_budget.reduced 时代表项可能进一步减少甚至为零，但频次/组间分布不变。
- 顶层 coverage.skipped_pairs 为未计算的二阶维度名对；原因为组合数量预算，不代表没有数据。

## 显式明细查询

`python3 query_profile.py --kind groups --dimensions channel --offset 0 --limit 10`

可用 kind=groups/time/slice；metrics 限定指标；filters 为类型化条件数组且 AND；start/end 左闭右开。查询仅作用于原结果。groups 返回完整分组描述及 slice；time 返回原粒度桶；slice 返回筛选行的分布。

分页字段 total_count、returned_count、has_more、next_offset。若字节预算限制，本页实际数量可能小于 limit。下一页沿用相同 query 和 data_sha256；快照改变不能合并旧页与新页。data_sha256 当前基于数据与 cube 的规范 JSON 序列化，不等于数据文件原始字节哈希。

明细中的 result_row_share 始终用完整原结果行数作分母，不因追加筛选改分母。统计明细不是结果原行，subagent 实际观察仍需指定原结果数据入口。

## 派发前不变量

- 两路全局行数一致；每项代表行数 + remaining_row_count = 全局行数。
- 全部窗口的 row_count 之和 = time.valid_time_count；represented_bucket_count = observed_bucket_count。
- 全量摘要覆盖不等于 subagent 已读覆盖。不同维度切片互相重叠，不能将占比直接相加。
- 两路仍无可信快照 ID；需从外部确认对应来源。行数一致不是快照一致的证明。
