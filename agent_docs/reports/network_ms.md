# `network_ms` 指标说明

**最后更新**：2026-03-31

## 目的

`network_ms` 是当前项目里用于衡量 **云端诊断工作节点 ↔ 本地 VCI 代理** 之间链路质量的核心指标。

它的设计目标不是去替代 `ping` 或 `tcping`，而是回答一个更贴近真实业务的问题：

> 在已经建立好的 VCI tunnel 上，一次真实代理调用往返，到底有多少时间花在“链路本身”上？

这个指标后续会被用于：

- 离线 benchmark 分析
- Local Zone / Region 部署选型对比
- 运行时 tunnel 质量监控
- `start_diagnostics` 前的网络质量闸门

## 一句话定义

```text
network_ms = duration_ms - hw_ms
```

其中：

- `duration_ms`：云端 `reverse_server` 视角看到的一次完整代理往返耗时
- `hw_ms`：本地 `reverse_client` 视角，J2534 / VCI / ECU 处理这次请求所花的时间
- `network_ms`：扣除硬件处理时间之后，剩余的 **tunnel 应用层有效往返时延**

请注意，这里的 `network_ms` 是：

- **应用层 tunnel 往返时延**
- **比 `ping` / `tcping` 更贴近真实业务**
- **不是纯物理传播时间**

## 它到底测了什么

一次真实请求的测量链路大致如下：

```text
Cloud GDS2 / virtual_j2534.dll
        │
        │ localhost:9001
        ▼
ReverseProxyServer
  ├─ 开始计时：duration_ms
  ├─ 通过既有 TCP tunnel 转发请求
  ▼
ReverseProxyClient
  ├─ 开始计时：hw_ms
  ├─ 调用 J2534 / VCI / ECU
  ├─ 结束计时：hw_ms
  └─ 把 hw_ms 作为 trailer 附在响应后面
        ▼
ReverseProxyServer
  ├─ 收到响应
  ├─ 解析 trailer 得到 hw_ms
  └─ 计算 network_ms = duration_ms - hw_ms
```

所以 `network_ms` 代表的是：

- 云端发出请求
- 请求穿过 tunnel 到本地
- 本地处理完成并返回
- 云端收到响应
- 再减掉本地硬件处理时间

之后剩下的那部分时间。

## 为什么不用 `ping` 或 `tcping` 直接替代

`ping` 和 `tcping` 依然有价值，但它们回答的问题不一样。

### `ping`

- 测的是 ICMP RTT
- 可能被运营商、路由器、防火墙限速或屏蔽
- 不代表真实业务流量路径一定相同

### `tcping`

- 通常测的是 TCP 建连耗时
- 比 `ping` 更接近 HTTP / tunnel / SSE 这类真实业务
- 很适合拿来做 **第一轮网络基线筛选**

### `network_ms`

- 测的是 **已经建好的 VCI tunnel 上的应用层真实往返**
- 使用的是项目自己的协议路径，而不是单独新建一个 TCP 连接
- 会自然反映：
  - tunnel 路径质量
  - 应用层排队与调度开销
  - 真实请求/响应节奏

所以：

> `tcping` 适合做“先验基线测试”；`network_ms` 更适合做“真实业务链路判断”。

## 代码里的测量实现

当前 `network_ms` 的实现主要分散在下面几个位置：

- `vci_proxy/benchmark.py`
  - 定义 timing trailer
  - 负责 JSONL benchmark event 的结构化输出
  - 负责 benchmark 汇总和 Markdown 报告生成
- `vci_proxy/reverse_client.py`
  - 在本地执行 J2534 调用时测量 `hw_ms`
  - 把 `hw_ms` 作为 trailer 附加到响应中
- `vci_proxy/reverse_server.py`
  - 在云端测量一次完整转发往返的 `duration_ms`
  - 解析 trailer，得到 `hw_ms`
  - 计算 `network_ms`
  - 记录到 benchmark log
  - 运行时定期发送 probe，维护 tunnel quality snapshot
- `vci_proxy/tunnel_quality.py`
  - 对运行时 probe 样本做滑动窗口统计
  - 给出 `good / warn / block`
  - 输出 `tunnel_quality.json`

## Benchmark 事件结构

结构化 benchmark 日志是 JSONL 格式，每行一个事件。典型字段如下：

- `run_label`
- `source`
- `started_at_s`
- `completed_at_s`
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

其中最关键的是这几个：

- `duration_ms`：总往返
- `hw_ms`：本地硬件/J2534执行
- `network_ms`：扣除 `hw_ms` 后的有效 tunnel 往返
- `cache_hit`：是否命中了服务端缓存

### 一个真实样例

来自 `bench_lag50.jsonl` 的样例事件：

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
- 其中本地硬件只用了 `0.913ms`
- 剩余 `171.087ms` 主要就是 tunnel 这条链路的成本

## Cache 命中时如何理解

`network_ms` 不能脱离 `cache_hit` 单独看。

当前 proxy 存在几类缓存/短路优化，包括：

- `ReadMsgs BUFFER_EMPTY` 短 TTL 缓存
- `StartFilter` 去重
- 部分只读 `IOCTL`

其中 `ReadMsgs BUFFER_EMPTY` 的默认 TTL 是 `150ms`。

这会带来两个结果：

1. 命中缓存的请求，往往会表现得“非常快”
2. 这些请求**不能**拿来代表真实网络质量

因此在解释 `network_ms` 时要遵守一个规则：

> **凡是 `cache_hit == true` 的样本，都不应该用于判断 tunnel 质量。**

这也是运行时网络质量闸门设计里明确采用的原则。

## `READ_MSGS` 为什么要拆成 empty / data

`GDS2` 会高频轮询 `PassThruReadMsgs`。

这些请求里大量是：

- `READ_MSGS_REQ(empty)`：没有真实车载数据，只是轮询噪声

只有一部分才是：

- `READ_MSGS_REQ(data)`：真的拿到了 ECU 数据

因此 benchmark 汇总时会把 `READ_MSGS_REQ` 进一步拆成：

- `READ_MSGS_REQ(empty)`
- `READ_MSGS_REQ(data)`

后者更适合拿来代表“真实数据面体验”。

## 运行时 `network_ms` 与离线 benchmark 的关系

当前项目里，`network_ms` 有两种使用方式。

### 1. 离线 benchmark

用途：

- 分析不同网络条件下的行为
- 对比 Local Zone / Region / 本地环境
- 生成实验报告

相关文件：

- `bench_cloud.jsonl`
- `bench_lag1.jsonl` ~ `bench_lag200.jsonl`
- `lag_report_new.md`
- `scripts/compare_proxy_benchmark.py`

### 2. 运行时 tunnel quality

用途：

- 诊断开始前做 pre-flight gate
- 会话进行中持续监控

实现方式：

- `reverse_server` 每 `3s` 发送一次轻量 `PING_REQ / PING_RSP` probe
- 不触发真实 J2534 硬件调用
- 使用最近 `5` 个样本做滑动窗口
- 快照写入：

```text
%PROGRAMDATA%\VCI_Proxy\tunnel_quality.json
```

当前运行时分级规则来自 `vci_proxy/tunnel_quality.py`：

- `good`：`p95 <= 80ms`
- `warn`：`80ms < p95 <= 150ms`
- `block`：`p95 > 150ms`

除此之外，以下情况也会直接视为 `block`：

- tunnel 未连接
- 样本不足
- snapshot 过期
- probe 失败

## 从现有 benchmark 文件里看到的经验结论

下面这些结论来自：

- `bench_cloud.jsonl`
- `bench_lag*.jsonl`
- `lag_report_new.md`
- `.sisyphus/drafts/network-quality-gating.md`
- `.sisyphus/drafts/network-ms-runtime-gating.md`

### 1. 基线 cloud run 很低

在 `bench_cloud.jsonl` 中：

- `READ_MSGS_REQ(data)` 平均 `network_ms` 约 `17.8ms`
- p95 约 `30.1ms`

这说明在理想近距离环境下，真正的 tunnel 成本很低。

### 2. `lag15` 左右仍然属于可接受范围

在 `bench_lag15.jsonl` 中：

- `READ_MSGS_REQ(data)` 平均 `network_ms` 约 `77.2ms`
- p95 约 `92.9ms`

这与当前运行时阈值中：

- `<= 80ms` 偏 `good`
- `80~150ms` 偏 `warn`

是大体一致的。

### 3. `lag20 ~ lag30` 已经明显退化，但仍可用

在 `bench_lag20.jsonl` 和 `bench_lag30.jsonl` 中：

- `READ_MSGS_REQ(data)` 平均 `network_ms` 大约 `113ms ~ 122ms`
- 仍能完成流程，但控制链路已经明显变慢

这类区间更像是：

- **能跑**
- **但用户会明显感觉慢**

### 4. `lag50+` 进入重度退化区

在 `bench_lag50.jsonl` 开始：

- `READ_MSGS_REQ(data)` 平均 `network_ms` 约 `182ms`
- 已经超过 `ReadMsgs BUFFER_EMPTY` 默认 `150ms` TTL

这意味着：

- 原本用于吸收空轮询噪声的缓存保护开始失效
- 更多空轮询请求不得不真的走完整 tunnel
- 整体系统压力与可感知卡顿都会上升

### 5. `lag150` 接近业务失败边界

在 `bench_lag150.jsonl` 中：

- `READ_MSGS_REQ(data)` 平均 `network_ms` 约 `364ms`
- `WRITE_MSGS_REQ` 平均 `network_ms` 约 `367ms`

这时 transport 可能还没完全断，但业务体验已经很差。

### 6. `lag200` 基本可视为业务不可用

在 `bench_lag200.jsonl` 中：

- `READ_MSGS_REQ(data)` 平均 `network_ms` 约 `493ms`
- 控制面 tail latency 已经非常高
- 缓存命中率明显塌陷
- 开始出现异常返回码和重试/重连迹象

因此：

> `lag200` 更像是“业务上不可用”，而不只是“网络有点慢”。

## 这个指标最适合怎么用

### 适合

- 比较不同部署点谁更接近本地 VCI 电脑
- 比较 Local Zone 与普通 Region 的实际效果
- 做 pre-flight 网络质量闸门
- 做 tunnel 运行中健康监控
- 做容量与缓存策略分析

### 不适合

- 直接当作纯物理 RTT
- 脱离 `cache_hit`、`msg_name`、`p95` 单独看
- 用单个样本决定整条链路好坏
- 用它替代最终端到端业务验证

## 如何阅读这个指标

推荐优先看下面几个维度：

1. **看 `READ_MSGS_REQ(data)`**
   - 它最能代表真实数据面体验
2. **看 `p95`，不要只看平均值**
   - 用户往往更容易感知 tail latency
3. **看 `cache_hit`**
   - 否则会被短路样本“美化”
4. **看控制面消息**
   - `OPEN_REQ`、`CONNECT_REQ`、`IOCTL_REQ`、`START_FILTER_REQ`
5. **结合窗口和趋势**
   - 单点值意义有限，连续窗口更重要

## 与 Texas → AWS Local Zone 选型的关系

如果当前只是想先验证：

> Texas 那台电脑到某个 AWS Local Zone 的网络延迟是否值得部署

那么建议分两步：

### 第一步：先用 `tcping`

用途：

- 快速筛选不同 Region / Local Zone
- 不需要先部署完整应用

### 第二步：再用 `network_ms`

用途：

- 在真实 VCI tunnel 上确认业务相关时延
- 观察缓存、控制面、数据面在真实负载下的表现

结论就是：

> `tcping` 用来做“先筛选”，`network_ms` 用来做“最终业务判断”。

## 相关文件索引

### 实现

- `vci_proxy/benchmark.py`
- `vci_proxy/reverse_client.py`
- `vci_proxy/reverse_server.py`
- `vci_proxy/tunnel_quality.py`

### 运行时闸门/会话侧

- `diagnostic_platform/runtime/session_preflight.py`

### 数据与报告

- `bench_cloud.jsonl`
- `bench_lag1.jsonl` ~ `bench_lag200.jsonl`
- `lag_report_new.md`

### 设计草稿

- `.sisyphus/drafts/network-quality-gating.md`
- `.sisyphus/drafts/network-ms-runtime-gating.md`

### 工具

- `scripts/compare_proxy_benchmark.py`

## 一句话总结

`network_ms` 不是普通的网络 ping 值，而是 **当前项目在真实 VCI tunnel 上、扣除本地 J2534 硬件执行后得到的应用层有效往返时延指标**；它比 `ping` / `tcping` 更贴近实际诊断体验，因此适合拿来做部署选型、运行时分级和业务可用性判断。
# Report Notice

This file is a supporting report, not the authoritative source of current architecture or API behavior. For current design docs, start with `agent_docs/README.md`.
