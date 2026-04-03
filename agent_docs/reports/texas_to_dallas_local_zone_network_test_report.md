# Texas 到 Dallas Local Zone 网络测试报告

## 文档角色

| 字段 | 内容 |
| --- | --- |
| 类型 | 支持性报告 / 实测网络报告 |
| 当前性 | 非权威架构文档；若与 `agent_docs/core/platform_architecture.md`、`agent_docs/ops/vci_proxy_and_tunnel.md` 或当前代码冲突，以后者为准 |
| 关注问题 | Texas 现场电脑到 AWS Dallas Local Zone 的基础网络质量是否足够好，值得继续部署项目做真实验证 |
| 测试日期 | 2026-04-01 |

## 一句话结论

Texas 测试电脑到 Dallas Local Zone 的基础网络质量非常好：延迟低、波动小、短时测试几乎无丢包，因此从部署选址角度看，Dallas Local Zone 非常值得继续推进。

## 测试背景

这次测试还没有接入真实硬件，也还没有部署完整的 `GDS2 + reverse_server + Flask` 业务链路。

因此它回答的问题不是：

- 完整业务链路是否已经验证通过

而是更基础的：

> Texas 现场电脑到 Dallas Local Zone 云主机，这条基础网络链路本身是否优秀，值得继续做项目级验证？

## 测试对象

| 项目 | 值 |
| --- | --- |
| 测试地点 | Aubrey, TX 76227 附近电脑 |
| 测试目标 | AWS Dallas Local Zone EC2 |
| 目标公网 IP | `18.88.12.108` |

## 测试范围

本次已覆盖：

- `ping`
- `tcping`
- HTTP 请求级响应时间
- `tracert`
- `pathping`

本次未覆盖：

- 真实硬件接入
- 完整 `GDS2 + reverse_server + Flask` 运行链路
- 真实运行时 `network_ms`

## 核心结果

### 1. 基础网络时延很低

| 测试项 | 最小值 | 平均值 | 最大值 | 丢包/失败 |
| --- | ---: | ---: | ---: | ---: |
| `ping` | `7ms` | `7ms` | `8ms` | `0%` |
| `tcping 3389` | `7.242ms` | `8.896ms` | `10.307ms` | `0%` |
| `tcping 8080` | `6.863ms` | `9.391ms` | `11.399ms` | `0%` |
| `tcping 9000` | `7.738ms` | `9.610ms` | `15.859ms` | `0%` |
| `tcping 8081` | `7.221ms` | `10.213ms` | `16.613ms` | `0%` |

这说明：

- Texas 到 Dallas Local Zone 的公网基础路径很理想
- 不只是 ICMP，真实 TCP 建连也很快
- 多个端口表现接近，说明不是单个端口偶然好看，而是整体链路质量较高

### 2. HTTP 层表现也很好

补充测试了 `8081` 端口上的 HTTP 请求，连续请求 20 次，结果全部成功。

关键数值：

- 首次请求：`160.55ms`
- 20 次整体平均：`34.65ms`
- 去掉首次冷启动后的稳态平均：`28.02ms`
- 大多数稳态请求落在：`25ms ~ 31ms`

这说明如果未来云端运行的是类似 Flask 这样的 HTTP 服务，那么在当前网络条件下，稳态请求大概率能维持在约 `30ms` 左右，这是很不错的水平。

### 3. 路由观察没有发现明显异常

`tracert` 与 `pathping` 的中间结果中，虽然存在公网常见的中间跳不响应或限速现象，但结合：

- `ping`
- `tcping`
- HTTP 稳态响应

更合理的判断是：

> 实际可用路径整体健康，没有看到明显的绕路或终点侧高延迟异常。

## 对当前项目的意义

### 1. Dallas Local Zone 很值得继续做项目级验证

从这次结果看，Dallas Local Zone 至少在“基础网络选址”这一层面是成立的。

这意味着：

- 后续若业务体验不理想，首先应该排查应用层、GDS2、硬件链路或 tunnel 实现
- 不应优先把问题归因到 Texas 到 Dallas 的公网基础时延

### 2. 真实 `network_ms` 大概率也会落在较优区间

虽然这次还没测到真实业务级 `network_ms`，但结合：

- 当前 `ping / tcping / HTTP` 结果
- 既有 benchmark 经验与分级阈值

可以先做一个保守估计：

- 未来真实业务下，`network_ms` 平均值大概率会落在 `15ms ~ 25ms`
- `p95` 很可能落在 `25ms ~ 35ms`

而当前运行时分级阈值是：

- `good`：`p95 <= 80ms`
- `warn`：`80ms < p95 <= 150ms`
- `block`：`p95 > 150ms`

因此 Dallas Local Zone 很有希望稳定落在 `good` 档。

## 建议的下一步

建议按下面顺序推进：

1. 在云端部署一个可持续运行的正式测试环境
2. 部署 `reverse_server` 与 Flask 服务
3. 接入 Texas 现场真实硬件后，实测项目级 `network_ms`
4. 最后验证完整 `GDS2 + reverse_server + Flask` 业务链路

这样推进的好处是：

- 可以先把“公网网络问题”和“应用服务问题”拆开
- 再把“应用服务问题”和“硬件链路问题”拆开
- 更容易定位最终瓶颈

## 与当前项目的关系

这份报告只回答“Texas 到 Dallas Local Zone 的基础网络是否优秀”。

它**不拥有**以下内容：

- 当前 Local Zone 部署建议：见 `agent_docs/reports/aws_local_zones_deployment.md`
- `network_ms` 指标定义与运行时 gating：见 `agent_docs/reports/network_ms.md`
- 当前项目架构：见 `agent_docs/core/platform_architecture.md`
- tunnel / proxy 子系统：见 `agent_docs/ops/vci_proxy_and_tunnel.md`

## 局限

- 这不是完整业务链路报告
- 这不是硬件接入后的实测报告
- 这不是对所有北美地区的泛化结论
- 这次结论主要适用于“Texas 测试点到 Dallas Local Zone”这条路径

## 相关文档

- `agent_docs/README.md`
- `agent_docs/reports/aws_local_zones_deployment.md`
- `agent_docs/reports/network_ms.md`
- `agent_docs/core/platform_architecture.md`
- `agent_docs/ops/vci_proxy_and_tunnel.md`
