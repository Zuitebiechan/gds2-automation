# VCI Proxy Benchmark Report — cloud_run

**Run label:** `cloud_run`

## Measurement Methodology

Each J2534 API call made by the cloud-side OEM diagnostic software
(e.g. GDS2) is intercepted at the Reverse Proxy Server and timed.

```
Cloud GDS2 ─► virtual_j2534.dll ─► localhost:9001
                                        │
                            ┌────────────▼────────────────┐
                            │  ReverseProxyServer          │
                            │  t_start ──────── t_end      │
                            │     duration_ms (monotonic)  │
                            └────────────┬────────────────┘
                                         │  TCP tunnel
                            ┌────────────▼────────────────┐
                            │  ReverseProxyClient (local)  │
                            │  hw_ms = J2534 driver time   │
                            │  ─► VCI ─► Vehicle           │
                            └─────────────────────────────┘
```

- **duration_ms**: Full round-trip measured with `time.monotonic()` at
  the proxy server — includes network transit (both legs) plus
  client-side J2534 hardware execution.
- **hw_ms**: J2534 driver execution time measured at the client with
  `time.monotonic()`. Reported only when the client sends a timing
  trailer (12-byte `TME0` magic + double).
- **network_ms**: `duration_ms − hw_ms` — pure network round-trip
  transit time (cloud → local + local → cloud).
- **Cache hits**: Certain high-frequency calls (ReadMsgs BUFFER_EMPTY,
  duplicate StartFilter, READ_VBATT) are served from server-side cache
  with `duration_ms = 0`.

## Overall Statistics

| Metric | Value |
|--------|-------|
| Total events | 7179 |
| Successful | 7178 (100.0%) |
| Cache hits | 5044 (70.3%) |
| Measurement window | 1022.7s |
| Overall request rate | 7.0 req/s |

## Latency by Message Type

### CONNECT_REQ

- **Requests:** 7 (success: 7, cache: 0)
- **Request rate:** 0.0 req/s

**Round-trip latency (ms):**

| Metric | min | avg | p50 | p95 | p99 | max |
|--------|-----|-----|-----|-----|-----|-----|
| duration | 16.0 | 27.0 | 16.0 | 53.4 | 61.1 | 63.0 |
| hw (J2534) | 3.6 | 14.1 | 3.9 | 38.9 | 43.8 | 45.1 |
| network | 12.2 | 17.4 | 15.2 | 25.7 | 26.8 | 27.1 |

### DISCONNECT_REQ

- **Requests:** 3 (success: 3, cache: 0)
- **Request rate:** 0.0 req/s

**Round-trip latency (ms):**

| Metric | min | avg | p50 | p95 | p99 | max |
|--------|-----|-----|-----|-----|-----|-----|
| duration | 16.0 | 21.0 | 16.0 | 29.5 | 30.7 | 31.0 |
| hw (J2534) | 3.4 | 3.6 | 3.4 | 4.0 | 4.1 | 4.1 |
| network | 11.9 | 17.4 | 12.6 | 26.1 | 27.3 | 27.6 |

### IOCTL_REQ

- **Requests:** 189 (success: 189, cache: 152)
- **Payload:** 740 bytes
- **Request rate:** 0.2 req/s

**Round-trip latency (ms):**

| Metric | min | avg | p50 | p95 | p99 | max |
|--------|-----|-----|-----|-----|-----|-----|
| duration | 0.0 | 3.7 | 0.0 | 16.0 | 32.9 | 78.0 |
| hw (J2534) | 0.8 | 5.1 | 1.5 | 23.9 | 51.3 | 58.5 |
| network | 11.4 | 14.7 | 14.3 | 18.9 | 23.6 | 24.9 |

### OPEN_REQ

- **Requests:** 10 (success: 10, cache: 0)
- **Request rate:** 0.0 req/s

**Round-trip latency (ms):**

| Metric | min | avg | p50 | p95 | p99 | max |
|--------|-----|-----|-----|-----|-----|-----|
| duration | 15.0 | 26.6 | 16.0 | 56.9 | 73.8 | 78.0 |
| hw (J2534) | 9.3 | 35.4 | 35.4 | 59.0 | 61.1 | 61.6 |
| network | 6.7 | 11.6 | 11.6 | 15.9 | 16.3 | 16.4 |

### READ_MSGS_REQ

- **Requests:** 6826 (success: 6825, cache: 4892)
- **J2534 messages:** 5527 (5.4 msg/s)
- **Payload:** 65,479 bytes
- **Request rate:** 6.7 req/s

**Round-trip latency (ms):**

| Metric | min | avg | p50 | p95 | p99 | max |
|--------|-----|-----|-----|-----|-----|-----|
| duration | 0.0 | 5.3 | 0.0 | 16.0 | 32.0 | 109.0 |
| hw (J2534) | 0.7 | 1.4 | 1.1 | 2.4 | 7.7 | 76.0 |
| network | 4.9 | 17.5 | 14.8 | 30.4 | 46.0 | 107.1 |

### READ_VERSION_REQ

- **Requests:** 3 (success: 3, cache: 0)
- **Request rate:** 0.0 req/s

**Round-trip latency (ms):**

| Metric | min | avg | p50 | p95 | p99 | max |
|--------|-----|-----|-----|-----|-----|-----|
| duration | 15.0 | 20.7 | 16.0 | 29.5 | 30.7 | 31.0 |
| hw (J2534) | 1.0 | 1.2 | 1.1 | 1.4 | 1.4 | 1.4 |
| network | 14.0 | 19.5 | 14.9 | 28.1 | 29.3 | 29.6 |

### START_FILTER_REQ

- **Requests:** 37 (success: 37, cache: 0)
- **Request rate:** 0.0 req/s

**Round-trip latency (ms):**

| Metric | min | avg | p50 | p95 | p99 | max |
|--------|-----|-----|-----|-----|-----|-----|
| duration | 15.0 | 21.6 | 16.0 | 31.2 | 32.0 | 32.0 |
| hw (J2534) | 3.0 | 4.8 | 4.1 | 8.4 | 13.9 | 15.1 |
| network | 8.4 | 16.8 | 12.4 | 27.7 | 28.2 | 28.3 |

### STOP_FILTER_REQ

- **Requests:** 35 (success: 35, cache: 0)
- **Request rate:** 0.0 req/s

**Round-trip latency (ms):**

| Metric | min | avg | p50 | p95 | p99 | max |
|--------|-----|-----|-----|-----|-----|-----|
| duration | 0.0 | 20.5 | 16.0 | 32.0 | 32.0 | 32.0 |
| hw (J2534) | 2.9 | 4.0 | 3.7 | 6.1 | 6.4 | 6.4 |
| network | 10.8 | 17.0 | 12.4 | 28.0 | 28.4 | 28.5 |

### WRITE_MSGS_REQ

- **Requests:** 69 (success: 69, cache: 0)
- **J2534 messages:** 69 (0.1 msg/s)
- **Request rate:** 0.1 req/s

**Round-trip latency (ms):**

| Metric | min | avg | p50 | p95 | p99 | max |
|--------|-----|-----|-----|-----|-----|-----|
| duration | 0.0 | 19.4 | 16.0 | 32.0 | 32.0 | 32.0 |
| hw (J2534) | 0.4 | 2.3 | 1.6 | 5.4 | 7.9 | 10.7 |
| network | 5.3 | 17.7 | 14.4 | 30.0 | 30.7 | 31.1 |

### READ_MSGS_REQ(data)

- **Requests:** 1096 (success: 1096, cache: 0)
- **J2534 messages:** 5527 (5.4 msg/s)
- **Payload:** 65,479 bytes
- **Request rate:** 1.1 req/s

**Round-trip latency (ms):**

| Metric | min | avg | p50 | p95 | p99 | max |
|--------|-----|-----|-----|-----|-----|-----|
| duration | 0.0 | 19.3 | 16.0 | 32.0 | 47.0 | 109.0 |
| hw (J2534) | 0.9 | 1.8 | 1.3 | 3.2 | 10.1 | 76.0 |
| network | 4.9 | 17.8 | 14.7 | 30.1 | 45.8 | 107.1 |

### READ_MSGS_REQ(empty)

- **Requests:** 5730 (success: 5729, cache: 4892)
- **Request rate:** 5.6 req/s

**Round-trip latency (ms):**

| Metric | min | avg | p50 | p95 | p99 | max |
|--------|-----|-----|-----|-----|-----|-----|
| duration | 0.0 | 2.6 | 0.0 | 16.0 | 31.0 | 94.0 |
| hw (J2534) | 0.7 | 1.0 | 1.0 | 1.3 | 1.6 | 4.4 |
| network | 11.6 | 17.1 | 15.0 | 30.8 | 46.0 | 93.0 |

## READ_MSGS Bucketing

GDS2 polls `PassThruReadMsgs` at high frequency. Most calls return
`BUFFER_EMPTY` (no vehicle data). To separate signal from noise,
READ_MSGS_REQ is split into two sub-buckets based on
`message_count` in the response:

- **READ_MSGS_REQ(empty)**: `message_count == 0` — polling noise
- **READ_MSGS_REQ(data)**: `message_count > 0` — actual vehicle data

| Bucket | Count | % of ReadMsgs | Avg latency |
|--------|-------|---------------|-------------|
| empty | 5730 | 83.9% | 2.6 ms |
| data | 1096 | 16.1% | 19.3 ms |

## Network vs Hardware Latency

When the client reports `hw_ms` (J2534 driver execution time),
we can decompose the round-trip into network transit and
hardware execution:

| Message | Avg duration (no cache) | Avg hw | Avg network | Network % |
|---------|------------------------|--------|-------------|-----------|
| CONNECT_REQ | 31.5 ms | 14.1 ms | 17.4 ms | 55% |
| DISCONNECT_REQ | 21.0 ms | 3.6 ms | 17.4 ms | 83% |
| IOCTL_REQ | 19.8 ms | 5.1 ms | 14.7 ms | 74% |
| OPEN_REQ | 47.0 ms | 35.4 ms | 11.6 ms | 25% |
| READ_MSGS_REQ | 18.9 ms | 1.4 ms | 17.5 ms | 92% |
| READ_VERSION_REQ | 20.7 ms | 1.2 ms | 19.5 ms | 94% |
| START_FILTER_REQ | 21.6 ms | 4.8 ms | 16.8 ms | 78% |
| STOP_FILTER_REQ | 21.1 ms | 4.0 ms | 17.0 ms | 81% |
| WRITE_MSGS_REQ | 20.0 ms | 2.3 ms | 17.7 ms | 88% |

## Cache Effectiveness

| Message | Total | Cache hits | Hit rate |
|---------|-------|------------|----------|
| IOCTL_REQ | 189 | 152 | 80.4% |
| READ_MSGS_REQ | 6826 | 4892 | 71.7% |

