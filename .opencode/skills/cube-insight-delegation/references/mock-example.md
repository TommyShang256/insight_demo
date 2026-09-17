# 当前 mock 的委派示例

本例展示如何依据已生成的 profile 规划，不声称已执行 OpenCode subagent 分析。
路径相对于仓库根目录：examples/sales/output/statistic_profile.json、examples/sales/output/dimension_profile.json、examples/sales/query_result.json、examples/sales/cube.json。

## 真实 profile 事实

- 查询结果 279 行，7 个字段；时间列 event_time，Asia/Shanghai，小时粒度。
- 时间 profile 保存全部 279 个观测桶，overview 最多展示 24 个代表原桶，无时间值缺失或解析失败。
- 查询覆盖 [2026-08-01, 2026-08-08) OR [2026-08-15, 2026-08-22)。中间一周被过滤排除，不能称为数据中断。
- channel 有 4 组：referral 100 行、organic 93 行、ads 85 行、NULL 1 行；全部已展示。
- region 有 3 组，全部展示。
- product_id 的 77 个组全部保存在 JSON；频次 Top-10 覆盖 65/279 行，但其余 67 组、214 行的统计也完整保留，可通过 index/detail 分页读取。
- channel × region 保存全部 10 个组合；product_id × region 保存全部 172 组，channel × product_id 保存全部 168 组。
- 结果中 channel 和 revenue 各有 1 个 NULL；缺失的是结果单元格，不说明源明细缺失。

## 用户只要求“整体观察”

先检查 279 行中相关列实际占用的上下文。在能装入预算时，一个综合任务即可，要求保留普通维度和时间键、描述两段查询范围内的现象。无需因有 3 个维度就启动几十个 agent。

结果字段 events 是原查询的计数结果列；可描述其返回值分布，但本任务不能新增“所有事件合计”业务指标。

## 用户要求“分渠道观察时间差异”且适合并行

可建立两个互斥主任务，而不是一值一个任务：

| 任务 | 数据选择 | 准确行数 | 关注问题 |
| --- | --- | ---: | --- |
| channels-a | channel 为 referral OR organic | 193 | 分别观察两个渠道在已返回时间段内的指标值变化 |
| channels-b | channel 为 ads OR NULL | 86 | 观察 ads；NULL 单行单独报告质量限制，不并入 ads |

193+86=279，全覆盖；行数来自该同维度完整分组，可以相加。每组读取完整两段查询范围，以保留时间比较上下文。两个渠道放进同一任务不等于合并它们的指标或曲线；region/product_id 保留为结果行定位和可比性依据。

对跨渠道比较问题，使用同一个比较任务或主 agent 核对同粒度证据；不能只把各渠道独立叙述拼接成因果解释。若增加 channel×region 补充任务，应标记与主任务重叠，不能把其覆盖加到 100% 上。

任务来源应绑定实际数据文件哈希与原始零基行索引。先用程序检查按类型化条件选择后的行数确为 193/86，再派发；不能把本示例数值套到后来更新的数据中。

## 三种反例

1. 只观察商品最高频 10 组，却结论写“所有商品均……”：覆盖仅 65/279，必须补取剩余数据或限制结论。
2. 认为 8 月 8—14 日未出现代表业务骤降：这是原 SQL 排除范围。
3. 将结果中各行 conversion_rate 平均后命名为整体转化率：只能称为这些返回单元格的均值，不能替代整体业务指标。
