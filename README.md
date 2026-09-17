# Insight demo：Cube Profiling

Python 3.9+，仅使用标准库。两路 profile 默认生成面向委派的紧凑摘要：全量计算，限制展开长度。输入是已执行 SQL 的结果，不反查底表、不重算业务指标。

```sh
python3 mock/generate.py
python3 statistic_profile.py
python3 dimension_profile.py
python3 -m unittest -v
```

默认输出 `mock/statistic_profile.json` 与 `mock/dimension_profile.json`。当前 mock 的紧凑 JSON：两路分别约 12 KB、16 KB，原详细结构同样紧凑编码约 453 KB、102 KB。压缩来自减少重复结构和逐项展开，不仅是去掉缩进。

## 默认摘要

StatisticProfile 3.0 保留数据质量、各指标全局分布，并用最多 24 个展示窗口覆盖全部有效时间结果。窗口优先不跨未观测间隔；段数超过预算时可打包，但明确标记 `spans_unobserved_gap`。窗口分位数直接由原结果值计算，极值附原数组行索引和时间。

DimensionProfile 2.0 对每个已计算的单维/二阶组合保留：

- 全量组数、空值、组大小分布、单例组规模、频次 Top-10 覆盖。
- 全部数值组的指标中位数/标准差分布，每组等权，明确区别于原结果行分布。
- 最多 8 个具体代表组，低基数时全部展示；高基数时轮转选高频组和指标组统计的两端及中间组，并保留选择原因与样本量。
- 代表组已展开/未展开的组数和行数，以及未计算的二阶组合。

`metric_schema` 声明指标顺序，`stats_columns` 声明统计列顺序，其他表也附列名。数组中的 NULL 仍保留。低基数渠道维度用几行表达，不再为每个指标反复嵌套长字段说明。

```python
from statistic_profile import statistic_profile
from dimension_profile import dimension_profile
statistic = statistic_profile(rows, cube, max_windows=24, max_bytes=16000)
dimension = dimension_profile(rows, cube, top_k=8, max_pairs=6, max_bytes=16000)
```

每路默认硬限制 16000 字节，两路总计最多 32000 字节（不含文件末尾换行）；使用实际紧凑 UTF-8 JSON 测量，不宣称是模型精确 token 数。输出记录 `output_budget`。时间超预算会减少窗口并重算，保持全部时间覆盖；维度超预算先减少组合代表项、再减少单维代表项，保留全量概况。连最小概况都超预算时明确报错，要求选择更少的指标/维度或提高预算，不静默删除定义。`--max-bytes 0` 显式关闭限制。

## 按需查询明细

不需要预先生成巨大的完整 profile 文件，直接在原查询结果上请求必要明细：

```sh
# 维度值列表与该页各组的收入分布
python3 query_profile.py --kind groups --dimensions channel --metrics revenue --limit 10

# 指定渠道、指定时段的原小时桶
python3 query_profile.py --kind time --metrics revenue --start '2026-08-15 00:00:00' --end '2026-08-17 00:00:00' --filters '[{"name":"channel","type":"string","value":"ads"}]'

# 仅描述该切片的收入分布
python3 query_profile.py --kind slice --metrics revenue --filters '[{"name":"channel","type":"null","value":null}]'
```

条件数组内 AND；时间左闭右开；时间解析遵守 cube 时区。分页返回 `total_count/returned_count/has_more/next_offset`；字节预算可能使本页条数小于 limit，应使用实际 next_offset 继续，并确认 `data_sha256` 与 `query` 相同。哈希覆盖数据及 cube 的规范序列化，不是文件字节哈希。行占比分母始终为完整输入行数。

详细函数 `statistic_profile_detail` 与 `dimension_profile_detail` 保留原结构，CLI 可显式加 `--detail`。这些入口不受摘要字节限制，不应默认传给主 agent。分页入口才适合获取高基数明细。

## 边界与验证

结果行占比不是收入贡献占比。转化率的分位数和均值只描述返回值，不等于整体转化率。摘要保留差异线索，但不保证代表组捕获所有现象；主 agent 需记录实际读取覆盖。摘要极值索引指向原文件数组，排序/切片后不要重新编号。

当前仍完整加载结果。新增摘要层复用原原子统计，高基数组间分布需计算全部组；尚未实现流式或数据库下推执行。列式 JSON 减少模型输入，不意味着减少所有计算内存。NULL/非法值、浮点精度、夏令时本地小时歧义等仍遵循原统计规则。

22 项测试覆盖原统计与 SQL 对照、时间分位数来自原值、全量组间分布、极值定位、间隔处理、预算缩减不丢覆盖、分页无重复遗漏及类型化 NULL 筛选。各原子方法保留中文说明与 MySQL 下推注释；未在真实 MySQL 上执行。

## OpenCode 委派 Skill

[项目 skill](.opencode/skills/cube-insight-delegation/SKILL.md)已适配新版本。可请求：

> 使用 cube-insight-delegation skill，读取 mock 中两路紧凑摘要，以 mock/events.json 为结果数据，规划并执行分渠道的时间差异观察。按需查询明细，明确覆盖范围并提供行级证据。

技能格式与示例数据可在本地验证，尚未执行真实 OpenCode subagent 调度。
