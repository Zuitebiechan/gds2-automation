# Reports Index

## 文档角色

本目录存放支持性报告，用来补充：

- 部署选址判断
- 网络与链路测量
- 历史实验结论

这些文件不是当前项目架构或 API 的权威定义。若与 `agent_docs/core/`、`agent_docs/ops/` 或当前代码冲突，以后者为准。

## 统一模板

本目录下的报告统一采用以下结构：

1. `## 文档角色`
2. `## 一句话结论`
3. `## 背景` 或 `## 测试背景`
4. `## 主要结论` 或 `## 核心结果`
5. `## 与当前项目的关系`
6. `## 局限`
7. `## 相关文档`

这样做的目的，是让支持性报告之间写法一致，同时避免与权威设计文档重复。

## 当前报告

| 文件 | 类型 | 主题 |
| --- | --- | --- |
| `agent_docs/reports/aws_local_zones_deployment.md` | 部署建议 | 北美场景下是否值得用 AWS Local Zone 承载当前诊断 worker |
| `agent_docs/reports/network_ms.md` | 指标说明 | `network_ms` 的定义、实现位置、解读方式与运行时用途 |
| `agent_docs/reports/texas_to_dallas_local_zone_network_test_report.md` | 实测报告 | Texas 到 Dallas Local Zone 的基础网络实测结果 |

## 与其他目录的边界

- 当前平台和代码结构：看 `agent_docs/core/`
- 当前运维、部署、tunnel 和 proxy：看 `agent_docs/ops/`
- 历史原型或已过时材料：放 `agent_docs/archive/`
- 原始 benchmark 工件：放 `reports/network_benchmarks/`

## 使用规则

阅读报告时请遵循：

1. 先看 `agent_docs/README.md`
2. 再看对应的权威设计文档
3. 最后把这里的报告当作“补充证据”或“实验记录”

如果报告与权威文档不一致，优先级应为：

1. 当前代码
2. `agent_docs/core/` 与 `agent_docs/ops/`
3. `README.md`
4. 本目录报告
