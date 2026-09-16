# Insight demo：Cube Profiling

Python 3.9+，仅使用标准库。输入为已执行 SQL 的结果对象列表及 cube 元数据，不执行过滤表达式，不反查底表。已实现 StatisticProfile 与 DimensionProfile，分别输出。

```sh
python3 mock/generate.py
python3 statistic_profile.py
python3 dimension_profile.py
python3 -m unittest -v
```

输出：`mock/statistic_profile.json` 和 `mock/dimension_profile.json`。自定义路径：

```sh
python3 statistic_profile.py --cube mock/cube.json --data mock/events.json --output mock/statistic_profile.json --max-time-buckets 1000
```

Python 调用：

```python
from statistic_profile import statistic_profile
from dimension_profile import dimension_profile
statistic = statistic_profile(rows, cube)
dimension = dimension_profile(rows, cube, top_k=10, max_pairs=6)
```
w
## 已实现

- 结果行数、字段观测类型、空值数与比例、完全重复结果的额外行数与重复组数。
- 指标结果值的数值样本量、空值、非数值、非有限值、零值和负值；极值、均值、总体标准差及 P05/P25/P50/P75/P95 线性插值分位数。
- 时间覆盖、无效时间数及逐桶行数与指标分布；支持 hour/day/month，按声明时区处理 ISO 字符串。仅输出观测桶，避免把过滤排除的一周当成缺失时间。
- 无时间维度、零行、全空值、非数值及无效时间等边界。

## 口径与限制

`distribution.mean` 是 SQL 结果单元格的描述性均值，不是业务整体指标。时间桶中的 `metrics.<id>.distribution` 也采用相同口径。profile 不生成业务汇总值，不要求公式、分子或分母；`aggregation` 仅保留原 SQL 的聚合类型作为说明。StatisticProfile 输出结构版本为 2.0，DimensionProfile 为 1.0。返回结果可能经过 HAVING/LIMIT，scope 将源总体完整性标记为 unknown，不能反推底层明细质量。

字段类型来自 Python 值的观测，空表/全 NULL 不猜测数据库类型；非空输入缺少必需列或各行列集合不一致时明确报错。输入仅支持 JSON 标量，日期使用 ISO 字符串。不执行任意 validity_sql，业务 valid_count 保持 unavailable，数值样本数单独报告。

第一版完整载入 JSON；分位数保留并排序样本，重复检测维护唯一结果集合，尚未实现流式或百万级压力验证。使用 Python 整数/浮点运算，不承诺十进制财务精度。时间序列默认最多返回 1000 桶，超出显式标记 truncated 与实际桶数，可调大 CLI 参数重新输出；未实现分页或连续空桶补齐。

当前 mock：279 行查询结果、280 个事件、279 个观测小时桶；源 SQL 的两个时间范围在 cube.time.filters 中以 OR 表示。

## MySQL 下推注释

`statistic_profile.py` 和 `dimension_profile.py` 的统计函数均附 MySQL 下推说明，按 MySQL 8.0+ 的 CTE 和窗口函数设计。所有查询从完整原 SQL 的结果 r 计算；建议物化/复用同一快照。普通计数/分布可合并为一条 SELECT；分位数通过 ROW_NUMBER、COUNT 窗口与相邻位置插值实现。辅助函数也注明与 SQL 的对应关系。

参考 MySQL 官方文档：[聚合函数](https://dev.mysql.com/doc/refman/8.4/en/aggregate-functions.html)、[窗口函数](https://dev.mysql.com/doc/refman/8.0/en/window-function-descriptions.html)。

16 项测试覆盖维度频次、空值组合、Top-K 覆盖守恒、稳定排序、组合预算、维度 SQL 分组对照，以及输入校验、空值、重复、分位数、时区、只含比率结果列的数据，以及聚合类型不改变统计行为；用 SQLite 独立查询核对全局描述统计、逐时间桶分布与分位数。MySQL 注释尚未在真实 MySQL 实例上运行；SQLite 对照不等价于 MySQL 集成测试。

## DimensionProfile 使用与字段

```sh
python3 dimension_profile.py --top-k 10 --max-pairs 6
```

- `single_dimensions`：每个普通维度的非空基数、空值行数、频次 Top-K 切片。
- `combinations`：默认最多 6 个二阶维度对，按单维实际组数（含 NULL）乘积及维度名排序选择。`--max-pairs 0` 禁用组合。
- 每个维度集合的 `group_count` 包含 NULL 组，`non_null_group_count` 不含任一维度为空的组；单维时后者就是非空基数。
- `groups[].slice`：带类型的维度名/值，区分 NULL、空字符串、真实 Other、数字和布尔值。`1` 与 `1.0` 合组，字符串 `"1"` 独立。
- `groups[].result_row_share`：该切片行数除以完整输入结果行数（含 NULL），不是指标贡献占比。比如 4 行中广告渠道出现 3 行，比例就是 0.75。
- `groups[].metrics`：复用现有分布算法描述切片的指标值；`time` 只给时间覆盖、有效/无效/空时间数及不同时间值数量，不展开逐桶数据。
- `coverage`：组级记录已展示与剩余组数、行数、占比及截断状态；顶层记录未执行组合与原因。没有合成的 Other 组。

Top-K 按频次降序，平局按类型化 JSON 键字典序排序，只限制展示，不改变统计分母。只为选中的组计算分布；空值组若不在 Top-K，仍计入概况和剩余覆盖。空输入的占比为 null；无普通维度的状态为 not_applicable。

维度为 NaN/Infinity 时拒绝输入，指标中的非有限值仍按 StatisticProfile 规则单独计数。当前完整持有输入并建立组到行引用的映射，Top-K 不限制输入内存；组合按顺序计算，尚未进行百万行压力验证。两路合并和 subagent 委派不在当前实现范围内。
