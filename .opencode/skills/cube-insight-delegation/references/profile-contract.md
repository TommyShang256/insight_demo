# 两路 Profile 字段契约

本 skill 消费现有结果，不要求先合并成新 schema。字段不存在时不可当作 0；不认识的结构版本先核对兼容性再委派。

## StatisticProfile 2.0

| 路径 | 委派用途与限制 |
| --- | --- |
| profile_type / schema_version | 应为 StatisticProfile / 2.0 |
| scope.dimensions / scope.time | 普通维度名、已执行过滤、时间列/时区/粒度；不能要求 cube_id |
| scope.source_completeness | 当前为 unknown_from_result_only，不能声称源总体完整 |
| dataset.result_row_count | 全局结果行数；不是源事件数 |
| dataset.fields | 观测类型、NULL 数和比例；空表/全空列不能推断声明类型 |
| dataset.duplicates.extra_row_count | 完全重复结果的额外行数，不能推断源明细重复；读取时保留 |
| metrics.<id>.column / aggregation | 列映射与原查询类型；aggregation 不触发再次计算业务值 |
| metrics.<id>.distribution | sample_count、non_null_count、finite_numeric_count、non_numeric_count、non_finite_count、null_count、zero_count、negative_count、min/max/mean/stddev_pop、quantiles |
| distribution.valid_count | 当前为 unavailable 状态对象；数值样本量看 finite_numeric_count |
| time.status | computed / not_applicable / unavailable，只有可用时安排时间任务 |
| time.min / max / null_count / invalid_count | 观测范围和时间质量，不保证中间连续 |
| time.grain / timezone | 比较时间需遵守；DATE/无偏移字符串按现有本地时间规则 |
| time.truncated / observed_bucket_count / returned_bucket_count | 输出桶是否完整；截断时不能只凭已返回桶描述全时段 |
| time.buckets[] | time、result_row_count、metrics.<id>.distribution；是各桶内结果值的分布，不是原始单一业务时间序列 |

分位数为 Type 7 线性插值，负数仍可为有限样本；均值是单元格均值。质量限制可以改变任务问题与证据强度，不能自动产生业务异常或因果标签。

## DimensionProfile 1.0

| 路径 | 委派用途与限制 |
| --- | --- |
| profile_type / schema_version | 应为 DimensionProfile / 1.0 |
| status | computed 或无普通维度时 not_applicable |
| scope.result_row_count | 应与 StatisticProfile.dataset.result_row_count 相等 |
| single_dimensions[] / combinations[] | 每项描述一个维度集合；combinations 当前为二阶 |
| item.dimensions | 普通维度列名列表，不含时间维度 |
| item.group_count | 含空值组的实际组数 |
| item.non_null_group_count | 全部分量均非 NULL 的组数；单维时即非空基数 |
| item.null_row_count / null_row_share | 任一组成维度为 NULL 的行数/全局结果行数 |
| item.groups[].slice | name/type/value 列表，组合条件为 AND；type 有 null/string/number/boolean |
| item.groups[].result_row_count / result_row_share | 当前切片行数/全局结果行数，分母包含 NULL 行 |
| item.groups[].metrics.<id> | column、aggregation、distribution；仅切片描述 |
| item.groups[].time | 覆盖极值、valid_time_count、distinct_time_count、null_count、invalid_count；没有逐桶切片序列 |
| item.coverage | top_k、selected/remaining_group_count、selected/remaining_row_count、selected/remaining_row_share、truncated、ordering |
| coverage.skipped_pairs | 未计算的维度对及原因；不是空数据或没有交互关系 |

Top-K 排序为频次降序、类型化 JSON 键字典序。组合挑选按组数乘积与维度名排序，仅用于成本限制。remaining_* 只说明剩余规模，不能据此编造剩余维度值、指标分布或假 Other 切片。

## 派发前检查的不变量

- 两路行数一致；每项维度 selected_row_count + remaining_row_count = 全局行数。
- 展示组行数之和等于 selected_row_count。正行数时行占比与行数/全局行数一致，浮点允许微小误差；零行时比例为 null。
- 某个切片的每个指标 distribution.sample_count 应等于切片行数；有效数值样本可能更少。
- 主划分组、NULL 组、剩余组形成覆盖；不同维度的覆盖不是互斥分片。
- 现有 schema 不含可信快照标识，需用外部来源信息/文件哈希或同次计算依据关联。只核对不变量不等于证明数据来源一致。

这些约束用于拒绝不一致输入，不是要求主 agent 在语言上下文中重做全部统计。使用程序提取字段和计数即可。
