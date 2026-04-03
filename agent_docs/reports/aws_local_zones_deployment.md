# 北美 AWS Local Zones 部署建议报告

## 文档角色

| 字段 | 内容 |
| --- | --- |
| 类型 | 支持性报告 / 部署建议 |
| 当前性 | 非权威架构文档；若与 `agent_docs/core/platform_architecture.md`、`agent_docs/ops/deployment_and_operations.md` 或当前代码冲突，以后者为准 |
| 关注问题 | 北美用户场景下，是否值得把当前诊断工作节点部署到 AWS Local Zone |
| 适用阶段 | 当前单 Worker、单活跃业务会话、`gds2` 为唯一生产 backend 的阶段 |

## 一句话结论

对当前代码库来说，最值得先验证的方案不是“拆成很多云服务”，而是把**整套 Windows 诊断工作节点**直接部署到靠近北美技师的 AWS Local Zone，再用真实会话数据验证是否显著改善交互延迟。

## 背景

当前仓库的实时敏感链路主要不是数据库，也不是通用 Web 请求，而是：

```text
技师本地电脑 <-> 云端诊断工作节点 <-> GDS2 / Java Agent / reverse tunnel
```

对当前产品阶段而言，延迟最敏感的部分包括：

- reverse-tunnel J2534 往返
- `/api/session/*`、`/api/diagnose/*`、`/api/navigate/*` 请求
- SSE 进度流
- live data / DTC 读取等交互操作

相对而言，AI Diagnose 由于存在 30 秒采集窗口，总耗时并不主要受纯 RTT 决定。

## 当前项目约束

这份建议以当前代码库的现实约束为前提：

- 当前支持的公开 API 仍是：
  - `/api/session/*`
  - `/api/diagnose/*`
  - `/api/navigate/*`
- 当前 worker 模型仍是：
  - `1 worker process = 1 active business session = 1 active backend bundle`
- 当前生产 backend 只有 `gds2`
- 当前云端工作节点已经承载关键实时组件：
  - GDS2
  - Java Agent
  - `python -m vci_proxy.reverse_server`
  - `python app.py --port 8080`

这些事实的权威说明见：

- `agent_docs/core/project_overview.md`
- `agent_docs/core/platform_architecture.md`
- `agent_docs/ops/deployment_and_operations.md`

## 主要结论

### 1. 当前阶段最合理的 Local Zone 方案是“整机靠近”

对当前代码库，最推荐的起点是：

- 不先拆 control plane / worker plane
- 不先引入额外微服务
- 先把**完整 Windows 诊断工作节点**放进一个目标 Local Zone

也就是把下面这些一起放进去：

- Windows EC2
- GDS2
- Java Agent
- `vci_proxy.reverse_server`
- Flask API
- SSE 能力

这比“为了云架构而过度拆分”更符合当前项目阶段。

### 2. Local Zone 最适合当前项目的原因是“实时交互链路短”

Local Zone 对本项目的价值，不在于它能让所有事情都变成个位数毫秒，而在于它有机会显著缩短：

- 技师本地电脑到云端工作节点
- 云端工作节点到本地 VCI 代理形成的整体交互回路

也就是说，它更像是一个**把实时会话节点向技师靠近**的部署工具。

### 3. 当前阶段不建议过早做“Region 控制平面 + 多 Local Zone Worker”大拆分

那个形态更适合未来真正进入多用户、调度、持久化、配额治理阶段之后再做。

对于当前仓库，优先级更高的是：

1. 先证明 Local Zone 对真实诊断交互有收益
2. 再决定是否值得进入多 Zone / 多 Worker 的平台化调度

## 建议部署形态

### 方案 A：当前阶段单 Worker 试点

```text
北美技师本地电脑
    |
    | HTTPS / SSE / reverse tunnel
    v
AWS Local Zone Windows EC2
    - GDS2
    - Java Agent
    - reverse_server
    - Flask API
```

这是最符合当前仓库的试点方式。

### 哪些内容应该放在 Local Zone

应放入 Local Zone 的，是所有直接影响实时诊断体验的组件：

- Windows EC2 worker
- GDS2
- Java Agent
- `python -m vci_proxy.reverse_server`
- `python app.py --port 8080`
- 与 `/api/session/*`、`/api/diagnose/*`、`/api/navigate/*` 直接相关的运行时

### 哪些内容可以留在父 Region

可以继续放在父 Region 的，是相对“耐延迟”的支撑资源：

- AMI / 快照
- CloudWatch 日志与告警
- SSM 自动化
- 构建产物或 S3
- 未来如果引入的控制平面服务

简化成一句话就是：

> 把“诊断会话工作节点”放到 Local Zone，把“耐延迟的管理与持久化资源”留在父 Region。

## 北美选址建议

Local Zone 更像“都市圈优化”，而不是“整个北美只选一个点就万事大吉”。

因此建议按用户分布来选：

### 如果首批用户偏东部

- 父 Region 可先从 `us-east-1` 起步
- 再评估东部或中东部 Local Zone

### 如果首批用户偏西部

- 父 Region 可先从 `us-west-2` 起步
- 再评估西部 Local Zone

### 如果用户分布较散

先做：

1. 一个父 Region
2. 一个 Local Zone 试点
3. 用真实会话数据验证收益

只有在收益明确后，再考虑第二个 Local Zone。

## 当前代码库下的实施建议

### Worker 镜像策略

优先使用预烘焙 AMI，而不是每次全量初始化。

AMI 至少应包含：

- GDS2
- Python 运行环境与项目代码
- Java 运行环境与 Agent
- `virtual_j2534.dll` 注册
- `reverse_server` 与 API 启动脚本

### Session 模型

当前阶段建议继续保持：

- 一台 Worker 对应一个活跃诊断会话

这与当前代码约束一致，也能最大程度降低 GDS2 单实例、副作用和桌面上下文耦合带来的风险。

## 预期收益

对于当前项目，更合理的收益预期是：

- DTC、live data、导航交互更灵敏
- 会话启动到可操作状态的时间更稳定
- SSE 反馈更及时

而不是期待：

- AI Diagnose 总时长会按 RTT 比例缩短

因为 AI 路径本身还有 30 秒采集窗口。

## 验证清单

在决定是否长期采用 Local Zone 之前，应至少验证：

1. 客户端到 worker 的 RTT
2. `POST /api/diagnose/start` 总耗时
3. `GET /api/diagnose/dtcs` 总耗时
4. live stream 从启动到第一条 SSE 的耗时
5. session start 到首个可操作状态的时间
6. reverse-tunnel 代表性请求/响应耗时
7. 10–20 分钟诊断会话稳定性

推荐验证顺序：

1. 启动一台 Local Zone Windows worker
2. 不改架构，只把当前整套 worker 迁过去
3. 用北美真实客户端做基线对比
4. 再决定是否值得扩到第二个 Local Zone

## 与当前项目的关系

这份报告只回答“部署位置是否值得优化”。

它**不拥有**以下内容：

- 当前平台分层定义：见 `agent_docs/core/platform_architecture.md`
- backend/capability 设计：见 `agent_docs/core/backend_architecture.md`
- 运行命令、端口、配置路径：见 `agent_docs/ops/deployment_and_operations.md`
- tunnel 与本地代理细节：见 `agent_docs/ops/vci_proxy_and_tunnel.md`

## 局限与注意事项

- Local Zone 是父 Region 的扩展，不是独立 Region
- 各 Local Zone 提供的实例类型和服务能力不同
- 当前报告是部署建议，不是已上线运行后的性能复盘
- 当前阶段不要为了“云原生美观”而过度拆分架构

## 相关文档

- `agent_docs/README.md`
- `agent_docs/core/project_overview.md`
- `agent_docs/core/platform_architecture.md`
- `agent_docs/ops/deployment_and_operations.md`
- `agent_docs/ops/vci_proxy_and_tunnel.md`

## 外部参考

- AWS Local Zones FAQ: <https://aws.amazon.com/about-aws/global-infrastructure/localzones/faqs/>
- AWS Local Zones locations: <https://aws.amazon.com/about-aws/global-infrastructure/localzones/locations/>
- AWS Local Zones User Guide: <https://docs.aws.amazon.com/local-zones/latest/ug/what-are-local-zones.html>
- Available Local Zones: <https://docs.aws.amazon.com/local-zones/latest/ug/available-local-zones.html>
- AWS Global Accelerator: <https://docs.aws.amazon.com/global-accelerator/latest/dg/what-is-global-accelerator.html>
- Route 53 latency-based routing: <https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/routing-policy-latency.html>
