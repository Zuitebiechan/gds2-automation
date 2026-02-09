"""
VCI Proxy 反向连接服务器（运行在阿里云）

接受本地 VCI Proxy 的反向连接，并提供本地代理服务。

架构:
  本地 VCI Proxy ──连接──▶ 此服务器 (端口 9000)
                              ↑
                         本地测试程序连接 localhost:9001
"""

import asyncio
import struct
import time
import logging
import argparse
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Message type names for logging
MSG_NAMES = {
    0x0001: "Open", 0x0002: "Close", 0x0003: "Connect", 0x0004: "Disconnect",
    0x0005: "ReadMsgs", 0x0006: "WriteMsgs", 0x0007: "Ioctl",
    0x0010: "StartFilter", 0x0011: "StopFilter",
    0x0020: "ReadVersion", 0x0021: "GetLastError", 0x00FF: "Heartbeat",
    0x8001: "Open_RSP", 0x8002: "Close_RSP", 0x8003: "Connect_RSP",
    0x8004: "Disconnect_RSP", 0x8005: "ReadMsgs_RSP", 0x8006: "WriteMsgs_RSP",
    0x8007: "Ioctl_RSP", 0x8010: "StartFilter_RSP", 0x8011: "StopFilter_RSP",
    0x8020: "ReadVersion_RSP", 0x8021: "GetLastError_RSP", 0x80FF: "Heartbeat_ACK",
}

MAGIC = 0x4A325334
HEADER_SIZE = 14

# Message types
MSG_HEARTBEAT = 0x00FF
MSG_HEARTBEAT_ACK = 0x80FF


class ReverseProxyServer:
    """反向代理服务器 - 接受 VCI Proxy 的连接"""

    def __init__(self, listen_port: int = 9000, proxy_port: int = 9001):
        self.listen_port = listen_port  # VCI Proxy 连接的端口
        self.proxy_port = proxy_port    # 本地程序连接的端口
        self.vci_reader: Optional[asyncio.StreamReader] = None
        self.vci_writer: Optional[asyncio.StreamWriter] = None
        self.vci_connected = asyncio.Event()
        self.vci_lock = asyncio.Lock()  # 保护 VCI 写操作
        self.request_queue = asyncio.Queue()
        self.response_futures = {}
        self.sequence = 0

    async def start(self):
        """启动服务器"""
        # 启动 VCI 监听服务
        vci_server = await asyncio.start_server(
            self._handle_vci_connection, '0.0.0.0', self.listen_port
        )
        logger.info(f"等待 VCI Proxy 连接到端口 {self.listen_port}...")

        # 启动代理服务
        proxy_server = await asyncio.start_server(
            self._handle_proxy_connection, '127.0.0.1', self.proxy_port
        )
        logger.info(f"代理服务监听端口 {self.proxy_port}")

        print(f"\n等待本地 VCI Proxy 连接到端口 {self.listen_port}...")
        print(f"连接后，可以通过 localhost:{self.proxy_port} 访问 J2534 设备\n")

        await asyncio.gather(
            vci_server.serve_forever(),
            proxy_server.serve_forever()
        )

    async def _handle_vci_connection(self, reader: asyncio.StreamReader,
                                     writer: asyncio.StreamWriter):
        """处理 VCI Proxy 的连接"""
        addr = writer.get_extra_info('peername')
        logger.info(f"VCI Proxy 已连接: {addr}")
        print(f"\n*** VCI Proxy 已连接: {addr} ***\n")

        self.vci_reader = reader
        self.vci_writer = writer
        self.vci_connected.set()

        try:
            while True:
                # 读取消息
                header = await reader.readexactly(HEADER_SIZE)
                magic, length, msg_type, sequence = struct.unpack('>IIHI', header)

                if magic != MAGIC:
                    logger.warning(f"无效的 Magic: {magic:#x}")
                    break

                body_len = length - HEADER_SIZE
                body = await reader.readexactly(body_len) if body_len > 0 else b''

                # 处理心跳消息 - 发送 ACK
                if msg_type == MSG_HEARTBEAT:
                    logger.debug(f"收到心跳, seq={sequence}")
                    ack_header = struct.pack('>IIHI', MAGIC, HEADER_SIZE, MSG_HEARTBEAT_ACK, sequence)
                    async with self.vci_lock:
                        writer.write(ack_header)
                        await writer.drain()
                    continue

                # 心跳 ACK - 忽略
                if msg_type == MSG_HEARTBEAT_ACK:
                    logger.debug(f"收到心跳ACK, seq={sequence}")
                    continue

                # 查找对应的 Future（响应消息）
                if sequence in self.response_futures:
                    future = self.response_futures.pop(sequence)
                    future.set_result((msg_type, body))
                else:
                    logger.warning(f"收到未知消息: type={msg_type:#x}, seq={sequence}")

        except asyncio.IncompleteReadError:
            logger.info(f"VCI Proxy 断开连接: {addr}")
        except Exception as e:
            logger.error(f"VCI 连接错误: {e}")
        finally:
            self.vci_connected.clear()
            self.vci_reader = None
            self.vci_writer = None
            writer.close()
            print(f"\n*** VCI Proxy 已断开 ***\n")

    async def _handle_proxy_connection(self, reader: asyncio.StreamReader,
                                       writer: asyncio.StreamWriter):
        """处理本地代理连接"""
        addr = writer.get_extra_info('peername')
        logger.info(f"代理客户端连接: {addr}")

        try:
            while True:
                # 等待 VCI 连接
                if not self.vci_connected.is_set():
                    logger.info("等待 VCI Proxy 连接...")
                    await self.vci_connected.wait()

                # 检查 VCI writer 是否有效
                if self.vci_writer is None:
                    logger.error("VCI writer 无效")
                    break

                # 读取请求
                header = await reader.readexactly(HEADER_SIZE)
                magic, length, msg_type, sequence = struct.unpack('>IIHI', header)

                if magic != MAGIC:
                    logger.warning(f"无效的 Magic: {magic:#x}")
                    break

                body_len = length - HEADER_SIZE
                body = await reader.readexactly(body_len) if body_len > 0 else b''

                # 转发请求到 VCI Proxy
                self.sequence += 1
                new_seq = self.sequence

                msg_name = MSG_NAMES.get(msg_type, f"0x{msg_type:04x}")
                fwd_start = time.monotonic()

                # 创建响应 Future
                future = asyncio.get_event_loop().create_future()
                self.response_futures[new_seq] = future

                # 重新打包并发送（使用锁保护）
                try:
                    new_header = struct.pack('>IIHI', MAGIC, length, msg_type, new_seq)
                    async with self.vci_lock:
                        if self.vci_writer is None:
                            raise ConnectionError("VCI 连接已断开")
                        self.vci_writer.write(new_header + body)
                        await self.vci_writer.drain()
                except Exception as e:
                    logger.error(f"转发请求失败: {e}")
                    self.response_futures.pop(new_seq, None)
                    break

                # 等待响应
                try:
                    resp_type, resp_body = await asyncio.wait_for(future, timeout=30.0)
                    fwd_ms = (time.monotonic() - fwd_start) * 1000

                    # 发送响应给客户端（使用原始 sequence）
                    resp_header = struct.pack('>IIHI', MAGIC,
                                             HEADER_SIZE + len(resp_body),
                                             resp_type, sequence)
                    writer.write(resp_header + resp_body)
                    await writer.drain()

                    # Log with latency (skip noisy ReadMsgs BUFFER_EMPTY)
                    if msg_type != 0x0005 or fwd_ms > 200:
                        logger.info(f"[PROXY] {msg_name} seq={sequence} -> {fwd_ms:.1f}ms")

                except asyncio.TimeoutError:
                    fwd_ms = (time.monotonic() - fwd_start) * 1000
                    logger.error(f"[PROXY] {msg_name} seq={sequence} TIMEOUT after {fwd_ms:.0f}ms")
                    break

        except asyncio.IncompleteReadError:
            logger.info(f"代理客户端断开: {addr}")
        except Exception as e:
            logger.error(f"代理连接错误: {e}")
        finally:
            writer.close()


def main():
    parser = argparse.ArgumentParser(description='VCI Proxy 反向连接服务器')
    parser.add_argument('--listen-port', type=int, default=9000,
                       help='VCI Proxy 连接端口 (默认: 9000)')
    parser.add_argument('--proxy-port', type=int, default=9001,
                       help='本地代理端口 (默认: 9001)')
    args = parser.parse_args()

    print("=" * 50)
    print("VCI Proxy 反向连接服务器")
    print("=" * 50)
    print(f"VCI Proxy 连接端口: {args.listen_port}")
    print(f"本地代理端口: {args.proxy_port}")
    print("=" * 50)

    server = ReverseProxyServer(args.listen_port, args.proxy_port)

    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        print("\n停止服务器...")


if __name__ == '__main__':
    main()
