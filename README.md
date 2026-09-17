# Insight demo：完整 Profile JSON + 分层读取

Python 标准库实现，无第三方依赖。两路 profile 保存完整统计，agent 通过一个读取工具按需获得表格；不使用数据库、会话账本或签名游标。

## 目录结构

```text
insight_demo/
├── profiling/                     # Python 包：算法与读取工具
│   ├── __init__.py
│   ├── statistic.py               # StatisticProfile
│   ├── dimension.py               # DimensionProfile
│   └── reader.py                  # 分层读取完整 JSON
├── tests/                         # 测试，与三个模块对应
├── examples/sales/                 # 独立、可复现的销售示例
│   ├── generate.py
│   ├── cube.json
│   ├── query.sql
│   ├── query_result.json
│   └── output/                    # 完整 profile JSON
├── docs/design.md                  # 设计与口径约定
├── .opencode/skills/               # OpenCode 委派 skill
└── README.md
```

以下命令在仓库根目录运行，无需安装包；通过 `python3 -m profiling.<模块>` 调用。核心计算函数接收内存数据，不读取示例目录；CLI 的默认输入/输出路径指向销售示例。

## 生成完整结果

```sh
python3 examples/sales/generate.py
python3 -m profiling.statistic
python3 -m profiling.dimension
python3 -m unittest discover -s tests -v
```

- `examples/sales/output/statistic_profile.json`：StatisticProfile 2.0，全局统计、全部时间桶及完整指标分布。
- `examples/sales/output/dimension_profile.json`：DimensionProfile 1.0，所有单维和已计算组合的全部组、频次、结果行占比、指标分布、时间覆盖。
- 默认最多计算 6 个二阶组合；未计算组合仍列入 `coverage.skipped_pairs`。这个计算范围限制不等于按 Top-K 丢弃组明细。

当前 mock 保存 279 个时间桶；商品的 77 个值及每个已计算组合的所有组均完整保存。`*_detail` 原子接口保留用于测试；正常调用 `statistic_profile(rows,cube)` / `dimension_profile(rows,cube,max_pairs=6)` 不截断组或桶。

## 唯一的分层读取工具

```sh
# 全量概况：全局指标分布、维度频次及组间差异、少量代表时间桶
python3 -m profiling.reader --level overview

# 指定维度值列表，简单分页
python3 -m profiling.reader --level index --dimension channel --limit 20
python3 -m profiling.reader --level index --dimension product_id --offset 20 --limit 20

# 指定维度值的收入明细；NULL 使用 --values '[null]'
python3 -m profiling.reader --level detail --dimension channel --values '["ads"]' --metrics revenue

# 指定范围的原时间桶；左闭右开
python3 -m profiling.reader --level index --start '2026-08-15' --end '2026-08-17'
python3 -m profiling.reader --level detail --start '2026-08-15' --end '2026-08-17' --metrics revenue --fields min p50 max

# 二阶组合，values 顺序对应 dimension 顺序
python3 -m profiling.reader --level detail --dimension channel region --values '["ads","east"]' --metrics revenue
```

返回 JSON 信封，`content` 是 Markdown 表格；`total_count/returned_count/has_more/next_offset` 表示分页。`detail` 每个“组或时间桶 × 指标”占一行；页偏移按实际返回表格行推进。

默认每页最多 20 行、最终响应最多 8192 字节（含 JSON 转义及末尾换行）。超限时减少本页行数；单行仍超限则报错，请限定指标或 `fields`。长字符串只在表格预览中标记截短，完整值仍在 JSON。`max_bytes` 是字节上限，不是 token 数；不维护跨调用累计预算。

Python 调用：

```python
from profiling.reader import read_profile
page = read_profile(level="detail", dimension=["channel"], values=["ads"],
                    metrics=["revenue"], fields=["min", "p50", "max"])
print(page["content"])
```

## 概况如何保持全局视野

工具只读取完整 profile JSON，不读取源结果、不执行 SQL：

- 全局指标分布直接取已保存统计。
- 维度组大小分布、频次 Top-10 覆盖和组间中位数分布，由全部已保存组统计得到。组间分布每组等权，不当作原结果行分布。
- 时间概况保留范围与桶数，展示最多 24 个原桶：首尾、指标极值所在桶，再补均匀位置。明确这是代表桶，不生成合并窗口，更不会平均小时分位数。
- 概况本身也可分页；具体细节通过 index/detail 获取，不建议自动把所有页拼进上下文。

## 边界

profile 描述原 SQL 返回值，不重新计算业务指标。结果行占比不是收入占比；转化率均值不等于整体转化率。原过滤已执行，不重复过滤。读取工具不支持从已有组摘要推导未保存的交叉切片，如“ads 在某时段的分布”；这种请求明确报错，不假造统计。

分页期间使用同一对 JSON，重新生成文件后从头读取；行数和维度/时间配置会校验，但没有快照或版本锁。每次调用完整解析两份文件到 Python 内存，控制的是模型响应长度，不宣称具备百万行流式读取。

20 项测试包括完整保存 10,000 个维度值与时间桶、受限表格响应、分页、NULL/字符串区别及原统计 SQL 对照。原子方法继续保留中文说明和 MySQL 下推注释。

OpenCode 委派说明见 [skill](.opencode/skills/cube-insight-delegation/SKILL.md)。工具当前提供 Python/CLI 入口，未注册真实 OpenCode 工具。subagent 观察对象仍是原查询结果切片；profile 读取工具不充当原数据读取器。
