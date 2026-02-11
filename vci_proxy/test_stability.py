"""
VCI Proxy 连接稳定性测试

模拟 GDS2 的实际操作流程：
1. PassThruOpen
2. PassThruConnect (ISO15765)
3. 保持连接，定期发送消息
4. 测试 ReadMsgs/WriteMsgs
5. PassThruDisconnect
6. PassThruClose
"""

import asyncio
import struct
import time
import sys

HOST = "127.0.0.1"
PORT = 9001

MAGIC = 0x4A325334
HEADER_SIZE = 14

# Message types
MSG_OPEN_REQ = 0x0001
MSG_OPEN_RSP = 0x8001
MSG_CLOSE_REQ = 0x0002
MSG_CLOSE_RSP = 0x8002
MSG_CONNECT_REQ = 0x0003
MSG_CONNECT_RSP = 0x8003
MSG_DISCONNECT_REQ = 0x0004
MSG_DISCONNECT_RSP = 0x8004
MSG_READ_MSGS_REQ = 0x0005
MSG_READ_MSGS_RSP = 0x8005
MSG_READ_VERSION_REQ = 0x0020
MSG_READ_VERSION_RSP = 0x8020

# Protocols
ISO15765 = 6
CAN = 5

sequence = 0

def write_uint32_be(val):
    return struct.pack('>I', val)

def write_uint16_be(val):
    return struct.pack('>H', val)

def read_uint32_be(buf, offset=0):
    return struct.unpack('>I', buf[offset:offset+4])[0]

async def send_recv(reader, writer, msg_type, body=b''):
    global sequence
    sequence += 1

    length = HEADER_SIZE + len(body)
    header = struct.pack('>IIHI', MAGIC, length, msg_type, sequence)

    start = time.time()
    writer.write(header + body)
    await writer.drain()

    # Read response
    resp_header = await asyncio.wait_for(reader.readexactly(HEADER_SIZE), timeout=30.0)
    magic, resp_len, resp_type, resp_seq = struct.unpack('>IIHI', resp_header)

    if magic != MAGIC:
        raise Exception(f"Invalid magic: {magic:#x}")

    body_len = resp_len - HEADER_SIZE
    resp_body = await reader.readexactly(body_len) if body_len > 0 else b''

    latency = (time.time() - start) * 1000
    return resp_type, resp_body, latency

async def test_stability():
    print("=" * 60)
    print("VCI Proxy 连接稳定性测试")
    print("=" * 60)
    print(f"目标: {HOST}:{PORT}")
    print()

    try:
        print("[1] 连接到代理服务器...")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(HOST, PORT),
            timeout=10.0
        )
        print("    ✓ TCP 连接成功")

        # PassThruOpen
        print("\n[2] PassThruOpen...")
        resp_type, resp_body, latency = await send_recv(reader, writer, MSG_OPEN_REQ)
        if len(resp_body) >= 8:
            ret_code = read_uint32_be(resp_body, 0)
            device_id = read_uint32_be(resp_body, 4)
            print(f"    ✓ ret={ret_code}, device_id={device_id}, latency={latency:.1f}ms")
            if ret_code != 0:
                print(f"    ✗ 错误: 返回码 {ret_code}")
                return
        else:
            print("    ✗ 响应太短")
            return

        # PassThruReadVersion
        print("\n[3] PassThruReadVersion...")
        body = write_uint32_be(device_id)
        resp_type, resp_body, latency = await send_recv(reader, writer, MSG_READ_VERSION_REQ, body)
        if len(resp_body) >= 244:
            ret_code = read_uint32_be(resp_body, 0)
            fw_ver = resp_body[4:84].rstrip(b'\x00').decode('utf-8', errors='replace')
            dll_ver = resp_body[84:164].rstrip(b'\x00').decode('utf-8', errors='replace')
            api_ver = resp_body[164:244].rstrip(b'\x00').decode('utf-8', errors='replace')
            print(f"    ✓ Firmware: {fw_ver}")
            print(f"    ✓ DLL: {dll_ver}")
            print(f"    ✓ API: {api_ver}")
            print(f"    ✓ latency={latency:.1f}ms")

        # PassThruConnect (ISO15765, 500kbps)
        print("\n[4] PassThruConnect (ISO15765, 500kbps)...")
        body = struct.pack('>IIII', device_id, ISO15765, 0, 500000)
        resp_type, resp_body, latency = await send_recv(reader, writer, MSG_CONNECT_REQ, body)
        if len(resp_body) >= 8:
            ret_code = read_uint32_be(resp_body, 0)
            channel_id = read_uint32_be(resp_body, 4)
            print(f"    ✓ ret={ret_code}, channel_id={channel_id}, latency={latency:.1f}ms")
            if ret_code != 0:
                print(f"    ✗ 错误: 返回码 {ret_code}")
        else:
            print("    ✗ 响应太短")
            channel_id = 0

        # 保持连接测试
        print("\n[5] 连接保持测试 (10秒, 每秒发送 ReadMsgs)...")
        success_count = 0
        fail_count = 0
        latencies = []

        for i in range(10):
            try:
                # PassThruReadMsgs
                body = struct.pack('>III', channel_id, 1, 100)  # channel, num_msgs, timeout_ms
                resp_type, resp_body, latency = await send_recv(reader, writer, MSG_READ_MSGS_REQ, body)

                if len(resp_body) >= 8:
                    ret_code = read_uint32_be(resp_body, 0)
                    num_msgs = read_uint32_be(resp_body, 4)
                    latencies.append(latency)
                    success_count += 1
                    print(f"    [{i+1}/10] ret={ret_code}, msgs={num_msgs}, latency={latency:.1f}ms")
                else:
                    fail_count += 1
                    print(f"    [{i+1}/10] ✗ 响应太短")

            except asyncio.TimeoutError:
                fail_count += 1
                print(f"    [{i+1}/10] ✗ 超时")
            except Exception as e:
                fail_count += 1
                print(f"    [{i+1}/10] ✗ 错误: {e}")

            await asyncio.sleep(1)

        # 统计
        print("\n[6] 测试统计:")
        print(f"    成功: {success_count}/10")
        print(f"    失败: {fail_count}/10")
        if latencies:
            print(f"    平均延迟: {sum(latencies)/len(latencies):.1f}ms")
            print(f"    最小延迟: {min(latencies):.1f}ms")
            print(f"    最大延迟: {max(latencies):.1f}ms")

        # PassThruDisconnect
        print("\n[7] PassThruDisconnect...")
        body = write_uint32_be(channel_id)
        resp_type, resp_body, latency = await send_recv(reader, writer, MSG_DISCONNECT_REQ, body)
        print(f"    ✓ latency={latency:.1f}ms")

        # PassThruClose
        print("\n[8] PassThruClose...")
        body = write_uint32_be(device_id)
        resp_type, resp_body, latency = await send_recv(reader, writer, MSG_CLOSE_REQ, body)
        print(f"    ✓ latency={latency:.1f}ms")

        writer.close()
        await writer.wait_closed()

        print("\n" + "=" * 60)
        if fail_count == 0:
            print("测试结果: ✓ 全部通过!")
        else:
            print(f"测试结果: ✗ {fail_count} 次失败")
        print("=" * 60)

    except asyncio.TimeoutError:
        print("✗ 连接超时")
    except ConnectionRefusedError:
        print("✗ 连接被拒绝 - 请确保 reverse_server.py 正在运行")
    except Exception as e:
        print(f"✗ 错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(test_stability())
