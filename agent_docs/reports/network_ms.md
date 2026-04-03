# `network_ms` 指标说明报告

## 文档角色

| 字段 | 内容 |
| --- | --- |
| 类型 | 支持性报告 / 指标说明 |
| 当前性 | 非权威架构文档；若与 `agent_docs/ops/vci_proxy_and_tunnel.md`、`agent_docs/core/runtime_flows.md` 或当前代码冲突，以后者为准 |
| 关注问题 | `network_ms` 到底测量什么、如何解读、如何用于部署选址与运行时 gating |
| 适用阶段 | 当前 reverse tunnel + J2534 + session preflight 设计 |

## 一句话结论

`network_ms` 不是普通的 `ping` 或 `tcping`，而是当前项目在**真实 VCI tunnel 请求往返中，扣除本地 J2534/硬件执行时间后得到的应用层有效链路时延**，因此更接近真实诊断体验。

## 背景

当前项目需要判断的不是“这台机器能不能 ping 通”，而是：

> 在已经建立好的 VCI tunnel 上，一次真实代理调用往返，究竟有多少时间花在链路本身上？

这也是 `network_ms` 后续被用于以下场景的原因：

- 离线 benchmark 对比
- Local Zone / Region 选址比较
- reverse tunnel 运行时健康监控
- `start_diagnostics` 前的网络质量闸门

## 指标定义

```text
network_ms = duration_ms - hw_ms
```

其中：

- `duration_ms`：云端 `reverse_server` 视角下，一次完整代理往返的总耗时
- `hw_ms`：本地 `reverse_client` 视角下，J2534 / VCI / ECU 处理这次请求的耗时
- `network_ms`：扣除本地硬件执行之后，剩下的 tunnel 应用层有效往返时延

## 它到底测量什么

一次典型请求的链路大致如下：

```text
Cloud GDS2 / virtual_j2534.dll
        ->
ReverseProxyServer
  -> 开始计时：duration_ms
  -> 通过既有 TCP tunnel 转发请求
ReverseProxyClient
  -> 开始计时：hw_ms
  -> 调用 J2534 / VCI / ECU
  -> 结束计时：hw_ms
  -> 将 hw_ms 作为 trailer 附到响应
ReverseProxyServer
  -> 收到响应
  -> 解析 trailer 得到 hw_ms
  -> 计算 network_ms = duration_ms - hw_ms
```

因此，`network_ms` 代表的是：

- 云端发起请求
- 请求穿过 tunnel 到本地
- 本地完成执行并回传
- 云端收到响应
- 扣掉本地硬件执行时间之后
- 剩下的链路有效往返成本

## 为什么不能只看 `ping` 或 `tcping`

### `ping`

- 测的是 ICMP RTT
- 可能被公网设备限速或屏蔽
- 不代表真实业务流量路径

### `tcping`

- 测的是 TCP 建连耗时
- 比 `ping` 更接近真实业务
- 很适合做第一轮选址筛选

### `network_ms`

- 测的是项目自己这条 tunnel 路径上的真实应用层往返
- 走的是现有业务通道，而不是额外新建一个测试连接
- 自然会体现：
  - tunnel 路径质量
  - 应用层排队与调度成本
  - 真实请求/响应节奏

因此可以把它理解为：

> `tcping` 更适合做“先筛候选点”，`network_ms` 更适合做“最终业务可用性判断”。

## 当前代码中的实现位置

`network_ms` 的实现和使用分散在以下位置：

- `vci_proxy/benchmark.py`
  - timing trailer
  - JSONL benchmark event 输出
  - benchmark 汇总与报告生成
- `vci_proxy/reverse_client.py`
  - 本地侧测量 `hw_ms`
  - 将 `hw_ms` 作为 trailer 附回
- `vci_proxy/reverse_server.py`
  - 云端测量 `duration_ms`
  - 解析 `hw_ms`
  - 计算 `network_ms`
  - 记录 benchmark
  - 周期性 probe 生成 tunnel quality snapshot
- `vci_proxy/tunnel_quality.py`
  - 对运行时 probe 样本做滑动窗口统计
  - 输出 `good / warn / block`
  - 写出 `tunnel_quality.json`

## Benchmark 事件结构

结构化 benchmark 日志为 JSONL，每行一个事件。关键字段包括：

- `run_label`
- `source`
- `duration_ms`
- `hw_ms`
- `network_ms`
- `msg_type` / `msg_name`
- `resp_type` / `resp_name`
- `status`
- `cache_hit`
- `channel_id`
- `return_code`
- `message_count`
- `payload_bytes`

其中最关键的是：

- `duration_ms`
- `hw_ms`
- `network_ms`
- `cache_hit`

### 真实样例

```json
{
  "run_label": "lag50",
  "source": "proxy_server",
  "duration_ms": 172.0,
  "hw_ms": 0.913,
  "network_ms": 171.087,
  "msg_name": "OPEN_REQ",
  "status": "success",
  "cache_hit": false
}
```

这表示：

- 整个往返用了 `172.0ms`
- 本地硬件执行只占 `0.913ms`
- 剩余 `171.087ms` 主要就是 tunnel 链路成本

## 解读时必须注意 `cache_hit`

`network_ms` 不能脱离 `cache_hit` 单独看。

当前 proxy 存在多类缓存/短路优化，例如：

- `ReadMsgs BUFFER_EMPTY` 短 TTL 缓存
- `StartFilter` 去重
- 部分只读 `IOCTL` 缓存

其中 `ReadMsgs BUFFER_EMPTY` 默认 TTL 为 `150ms`。

这意味着：

1. 命中缓存的请求会显得“非常快”
2. 这些样本**不能**用于判断 tunnel 真实质量

因此解读规则应是：

> 凡是 `cache_hit == true` 的样本，都不应该拿来作为 tunnel 质量判断的主依据。

## 为什么 `READ_MSGS` 要拆成 empty / data

GDS2 会高频轮询 `PassThruReadMsgs`。

其中大量请求其实只是：

- `READ_MSGS_REQ(empty)`：没有真实 ECU 数据，只是在轮询

真正更能代表数据面体验的，是：

- `READ_MSGS_REQ(data)`：确实拿到了 ECU 数据

因此 benchmark 汇总时，需要把 `READ_MSGS_REQ` 继续拆分成：

- `READ_MSGS_REQ(empty)`
- `READ_MSGS_REQ(data)`

后者更适合作为“真实诊断数据面体验”的代表。

## 运行时 tunnel quality 与离线 benchmark 的关系

当前项目里，`network_ms` 有两类主要用法。

### 1. 离线 benchmark

用于：

- 比较不同部署点
- 比较不同网络条件
- 生成实验报告

常见输入工件包括：

- `bench_cloud.jsonl`
- `bench_lag1.jsonl` ~ `bench_lag200.jsonl`
- `reports/network_benchmarks/`

### 2. 运行时 tunnel quality

用于：

- `start_diagnostics` 前的 preflight gate
- 会话进行中的持续健康监控

当前运行时做法是：

- `reverse_server` 每 3 秒发送一次轻量 probe
- 不触发真实 J2534 硬件调用
- 取最近 5 个样本做滑动窗口
- 将快照写到：

```text
%PROGRAMDATA%\VCI_Proxy\tunnel_quality.json
```

当前分级阈值为：

- `good`：`p95 <= 80ms`
- `warn`：`80ms < p95 <= 150ms`
- `block`：`p95 > 150ms`

此外，下列情况也会直接视为 `block`：

- tunnel 未连接
- 样本不足
- snapshot 过期
- probe 失败

## 从现有 benchmark 可得到的经验结论

### 1. 基线 cloud run 很低

在 `bench_cloud.jsonl` 中：

- `READ_MSGS_REQ(data)` 平均 `network_ms` 约 `17.8ms`
- p95 约 `30.1ms`

这说明近距离理想部署下，tunnel 本身成本不高。

### 2. `lag15` 大致仍在可接受范围

在 `bench_lag15.jsonl` 中：

- `READ_MSGS_REQ(data)` 平均约 `77.2ms`
- p95 约 `92.9ms`

它已经接近 `good / warn` 边界，但仍具可用性。

### 3. `lag20 ~ lag30` 已明显退化，但仍可运行

在这一段：

- `READ_MSGS_REQ(data)` 平均大约 `113ms ~ 122ms`

说明流程仍能跑，但用户会明显感到变慢。

### 4. `lag50+` 进入严重退化区

在 `bench_lag50.jsonl` 中：

- `READ_MSGS_REQ(data)` 平均约 `182ms`

这一数值已经超过 `ReadMsgs BUFFER_EMPTY` 默认 `150ms` TTL，意味着缓存保护开始失效，整体链路压力和卡顿感都会上升。

### 5. `lag150` 接近业务失败边界

在 `bench_lag150.jsonl` 中：

- `READ_MSGS_REQ(data)` 平均约 `364ms`
- `WRITE_MSGS_REQ` 平均约 `367ms`

此时 transport 可能还没彻底断，但业务体验已经很差。

### 6. `lag200` 基本可视为业务不可用

在 `bench_lag200.jsonl` 中：

- `READ_MSGS_REQ(data)` 平均约 `493ms`

并且开始出现：

- tail latency 明显升高
- 缓存收益下降
- 异常返回码、重试、重连迹象

## 这个指标最适合怎么用

### 适合

- 比较不同部署点谁更接近本地 VCI 电脑
- 对比 Local Zone 与普通 Region
- 做 preflight 网络闸门
- 做 tunnel 运行时健康监控
- 分析缓存策略与链路容量

### 不适合

- 直接当成纯物理 RTT
- 脱离 `cache_hit`、`msg_name`、`p95` 单独看
- 用单个样本判断整条链路质量
- 替代最终端到端业务验证

## 与当前项目的关系

这份报告只解释 `network_ms` 指标本身。

它**不拥有**以下内容：

- 当前 tunnel / proxy 子系统设计：见 `agent_docs/ops/vci_proxy_and_tunnel.md`
- session preflight 与 network gate 行为：见 `agent_docs/core/runtime_flows.md`
- 当前项目整体架构：见 `agent_docs/core/platform_architecture.md`

## 局限

- 该指标仍是项目内部指标，不应被误认为通用公网性能指标
- 不同消息类型的可比性有限，必须结合 `msg_name` 解读
- benchmark 与运行时 probe 的用途不同，不能直接混用

## 相关文档

- `agent_docs/README.md`
- `agent_docs/core/platform_architecture.md`
- `agent_docs/core/runtime_flows.md`
- `agent_docs/ops/vci_proxy_and_tunnel.md`
- `agent_docs/reports/texas_to_dallas_local_zone_network_test_report.md`

## 相关工件

- `reports/network_benchmarks/`
- `vci_proxy/benchmark.py`
- `vci_proxy/reverse_client.py`
- `vci_proxy/reverse_server.py`
- `vci_proxy/tunnel_quality.py`
