# 完整 JSON 与分层读取契约

保存格式为 StatisticProfile 2.0、DimensionProfile 1.0；完整时间桶、维度值和指标分布留在 JSON，不应整个注入 agent 上下文。

## 唯一读取入口

`python3 -m profiling.reader --level overview`

参数：level=overview/index/detail；dimension 为普通维度名列表；values 为同顺序 JSON 值数组；metrics 为指标 ID；fields 为统计字段；start/end 为左闭右开时间范围；offset/limit 简单分页。文件路径可由 --statistic / --dimension-profile 指定。

- overview 返回全局概况和全量组间分布。最多 24 个代表原时间桶，不是合并窗口；原桶分位数不能平均成窗口分位数。
- index + dimension 列出所有保存的组，含 NULL 与行占比；无 dimension 列时间桶。时间范围只能用于时间桶，不支持对维度组摘要追加时间筛选。
- detail 按对象 × 指标返回已存完整统计，可指定 fields 限定。默认含计数、极值、均值、标准差、P05/P25/P50/P75/P95；valid_count、population 等可以显式选择。
- content 为表格；total_count、returned_count、has_more、next_offset 说明当前页覆盖。响应超长会减少页条数，下一页必须使用实际 next_offset。
- 没有会话预算、数据库或签名游标。使用同一对文件，更新后从头分页；不要无目的地读取全部页。长值预览被截短时不能当成精确筛选值。

## 字段含义

StatisticProfile：dataset.result_row_count 为结果行数；dataset.fields 为结果字段质量；metrics.<id>.column 是列映射；distribution 是结果单元格分布；time.buckets 保存全部原桶，各桶也只描述其中的返回值。

DimensionProfile：single_dimensions/combinations 每项含 dimensions、group_count、non_null_group_count、null_row_count 和完整 groups。groups[].slice 为带类型的 name/type/value；result_row_share=组行数/全部结果行数，含 NULL 分母；metrics 为该组的完整分布；time 为覆盖范围。

组间中位数分布每组等权，不等同于按结果行加权的整体指标分布。指标 aggregation 仅说明原 SQL，不允许重新合成业务指标。valid_count=unavailable 不等于零数值样本，查看 finite_numeric_count。

两路行数及维度/时间配置必须一致；行数一致不证明文件来自同次生成，应由调用者保证对应关系。正常生成保存全部组/桶；兼容旧文件时 coverage.truncated/time.truncated 若为 true，须明确缺失范围。未计算组合见 coverage.skipped_pairs。

工具不读取源数据、不执行 SQL；没有保存的交叉切片不能从分布摘要准确推导。subagent 实际观察仍需要原结果数据入口。
