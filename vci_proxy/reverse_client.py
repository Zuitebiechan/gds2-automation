"""
VCI Proxy 反向连接模式

本地主动连接到阿里云，建立隧道，让云端可以使用本地的 J2534 设备。

架构:
  本地 VCI Proxy ──主动连接──▶ 阿里云:9000
                                   ↑
                              云端程序连接这里
"""

import asyncio
import socket
import struct
import time
import logging
import argparse
from typing import Optional

# 添加父目录到路径
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vci_proxy.protocol import MAGIC, HEADER_SIZE, MsgType, Message, ProtocolEncoder, ProtocolDecoder
from vci_proxy.j2534_driver import J2534Driver

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
}


class ReverseProxyClient:
    """反向代理客户端 - 主动连接到云服务器"""

    def __init__(self, server_host: str, server_port: int, dll_path: Optional[str] = None):
        self.server_host = server_host
        self.server_port = server_port
        self.dll_path = dll_path
        self.driver: Optional[J2534Driver] = None
        self.running = False

    def _ensure_driver(self) -> bool:
        """确保驱动已加载"""
        if self.driver is None:
            try:
                self.driver = J2534Driver(self.dll_path)
                logger.info(f"J2534 驱动已加载: {self.driver.dll_path}")
                return True
            except Exception as e:
                logger.error(f"加载 J2534 驱动失败: {e}")
                return False
        return True

    async def connect_and_serve(self):
        """连接到服务器并处理请求"""
        if not self._ensure_driver():
            return

        self.running = True
        retry_count = 0
        max_retries = 10

        while self.running and retry_count < max_retries:
            try:
                logger.info(f"正在连接到 {self.server_host}:{self.server_port}...")

                reader, writer = await asyncio.open_connection(
                    self.server_host, self.server_port
                )

                logger.info("已连接到云服务器!")
                retry_count = 0  # 重置重试计数

                # 发送注册消息
                await self._send_registration(writer)

                # 处理请求循环
                await self._handle_requests(reader, writer)

            except ConnectionRefusedError:
                retry_count += 1
                logger.warning(f"连接被拒绝，{5}秒后重试 ({retry_count}/{max_retries})...")
                await asyncio.sleep(5)
            except Exception as e:
                retry_count += 1
                logger.error(f"连接错误: {e}，{5}秒后重试 ({retry_count}/{max_retries})...")
                await asyncio.sleep(5)

        logger.info("已停止")

    async def _send_registration(self, writer: asyncio.StreamWriter):
        """发送注册消息"""
        # 使用心跳作为注册确认
        msg = ProtocolEncoder.encode_heartbeat(0)
        writer.write(msg)
        await writer.drain()
        logger.info("已发送注册消息")

    async def _handle_requests(self, reader: asyncio.StreamReader,
                               writer: asyncio.StreamWriter):
        """处理来自服务器的请求"""
        while self.running:
            try:
                # 读取消息头
                header = await asyncio.wait_for(
                    reader.readexactly(HEADER_SIZE),
                    timeout=60.0
                )

                magic, length, msg_type, sequence = Message.decode_header(header)

                if magic != MAGIC:
                    logger.warning(f"无效的 Magic: {magic:#x}")
                    break

                # 读取消息体
                body_len = length - HEADER_SIZE
                body = await reader.readexactly(body_len) if body_len > 0 else b''

                # 处理请求
                response = await self._handle_message(msg_type, body, sequence)

                if response:
                    writer.write(response)
                    await writer.drain()

            except asyncio.TimeoutError:
                # 发送心跳保活
                msg = ProtocolEncoder.encode_heartbeat(0)
                writer.write(msg)
                await writer.drain()
            except asyncio.IncompleteReadError:
                logger.info("服务器断开连接")
                break
            except Exception as e:
                logger.error(f"处理请求错误: {e}")
                break

    async def _handle_message(self, msg_type: int, body: bytes,
                             sequence: int) -> Optional[bytes]:
        """处理消息"""
        msg_name = MSG_NAMES.get(msg_type, f"0x{msg_type:04x}")
        start = time.monotonic()

        if msg_type == MsgType.HEARTBEAT:
            return ProtocolEncoder.encode_heartbeat_ack(sequence)

        elif msg_type == MsgType.HEARTBEAT_ACK:
            return None  # 不需要响应

        elif msg_type == MsgType.OPEN_REQ:
            device_name = ProtocolDecoder.decode_open_req(body)
            logger.info(f">> PassThruOpen({device_name})")
            ret, device_id = self.driver.open(device_name)
            ms = (time.monotonic() - start) * 1000
            logger.info(f"<< PassThruOpen -> ret={ret}, id={device_id} ({ms:.1f}ms)")
            return ProtocolEncoder.encode_open_rsp(ret, device_id, sequence)

        elif msg_type == MsgType.CLOSE_REQ:
            device_id = ProtocolDecoder.decode_close_req(body)
            logger.info(f">> PassThruClose({device_id})")
            ret = self.driver.close(device_id)
            ms = (time.monotonic() - start) * 1000
            logger.info(f"<< PassThruClose -> ret={ret} ({ms:.1f}ms)")
            return ProtocolEncoder.encode_close_rsp(ret, sequence)

        elif msg_type == MsgType.CONNECT_REQ:
            device_id, protocol_id, flags, baudrate = ProtocolDecoder.decode_connect_req(body)
            logger.info(f">> PassThruConnect(dev={device_id}, proto={protocol_id}, baud={baudrate})")
            ret, channel_id = self.driver.connect(device_id, protocol_id, flags, baudrate)
            ms = (time.monotonic() - start) * 1000
            logger.info(f"<< PassThruConnect -> ret={ret}, ch={channel_id} ({ms:.1f}ms)")
            return ProtocolEncoder.encode_connect_rsp(ret, channel_id, sequence)

        elif msg_type == MsgType.DISCONNECT_REQ:
            channel_id = ProtocolDecoder.decode_disconnect_req(body)
            logger.info(f">> PassThruDisconnect({channel_id})")
            ret = self.driver.disconnect(channel_id)
            ms = (time.monotonic() - start) * 1000
            logger.info(f"<< PassThruDisconnect -> ret={ret} ({ms:.1f}ms)")
            return ProtocolEncoder.encode_disconnect_rsp(ret, sequence)

        elif msg_type == MsgType.READ_MSGS_REQ:
            channel_id, num_msgs, timeout = ProtocolDecoder.decode_read_msgs_req(body)
            loop = asyncio.get_event_loop()
            ret, messages = await loop.run_in_executor(
                None, self.driver.read_msgs, channel_id, num_msgs, timeout
            )
            ms = (time.monotonic() - start) * 1000
            if ret != 0x10:  # Skip BUFFER_EMPTY noise
                logger.info(f"<< ReadMsgs(ch={channel_id}) -> ret={ret}, n={len(messages)} ({ms:.1f}ms)")
            return ProtocolEncoder.encode_read_msgs_rsp(ret, messages, sequence)

        elif msg_type == MsgType.WRITE_MSGS_REQ:
            channel_id, messages, timeout = ProtocolDecoder.decode_write_msgs_req(body)
            logger.info(f">> WriteMsgs(ch={channel_id}, n={len(messages)}, t={timeout})")
            loop = asyncio.get_event_loop()
            ret, num_written = await loop.run_in_executor(
                None, self.driver.write_msgs, channel_id, messages, timeout
            )
            ms = (time.monotonic() - start) * 1000
            logger.info(f"<< WriteMsgs -> ret={ret}, written={num_written} ({ms:.1f}ms)")
            return ProtocolEncoder.encode_write_msgs_rsp(ret, num_written, sequence)

        elif msg_type == MsgType.READ_VERSION_REQ:
            device_id = ProtocolDecoder.decode_read_version_req(body)
            logger.info(f">> PassThruReadVersion({device_id})")
            ret, fw, dll, api = self.driver.read_version(device_id)
            ms = (time.monotonic() - start) * 1000
            logger.info(f"<< ReadVersion -> ret={ret} ({ms:.1f}ms)")
            return ProtocolEncoder.encode_read_version_rsp(ret, fw, dll, api, sequence)

        elif msg_type == MsgType.START_FILTER_REQ:
            channel_id, filter_type, mask_msg, pattern_msg, flow_msg = \
                ProtocolDecoder.decode_start_filter_req(body)
            logger.info(f">> StartMsgFilter(ch={channel_id}, type={filter_type})")
            ret, filter_id = self.driver.start_msg_filter(
                channel_id, filter_type, mask_msg, pattern_msg, flow_msg
            )
            ms = (time.monotonic() - start) * 1000
            logger.info(f"<< StartMsgFilter -> ret={ret}, fid={filter_id} ({ms:.1f}ms)")
            return ProtocolEncoder.encode_start_filter_rsp(ret, filter_id, sequence)

        elif msg_type == MsgType.STOP_FILTER_REQ:
            channel_id, filter_id = ProtocolDecoder.decode_stop_filter_req(body)
            logger.info(f">> StopMsgFilter(ch={channel_id}, filter={filter_id})")
            ret = self.driver.stop_msg_filter(channel_id, filter_id)
            ms = (time.monotonic() - start) * 1000
            logger.info(f"<< StopMsgFilter -> ret={ret} ({ms:.1f}ms)")
            return ProtocolEncoder.encode_stop_filter_rsp(ret, sequence)

        elif msg_type == MsgType.IOCTL_REQ:
            channel_id, ioctl_id, input_data = ProtocolDecoder.decode_ioctl_req(body)
            logger.info(f">> PassThruIoctl(ch={channel_id}, ioctl=0x{ioctl_id:02x})")
            ret, output_data = self.driver.ioctl(channel_id, ioctl_id, input_data)
            ms = (time.monotonic() - start) * 1000
            logger.info(f"<< Ioctl(0x{ioctl_id:02x}) -> ret={ret} ({ms:.1f}ms)")
            return ProtocolEncoder.encode_ioctl_rsp(ret, output_data, sequence)

        else:
            logger.warning(f"未知消息类型: {msg_type:#x}")
            return None

    def stop(self):
        """停止客户端"""
        self.running = False


def main():
    parser = argparse.ArgumentParser(description='VCI Proxy 反向连接客户端')
    parser.add_argument('--host', default='8.136.197.36', help='云服务器地址')
    parser.add_argument('--port', '-p', type=int, default=9000, help='云服务器端口')
    parser.add_argument('--dll', default=None, help='J2534 DLL 路径')
    args = parser.parse_args()

    print("=" * 50)
    print("VCI Proxy 反向连接模式")
    print("=" * 50)
    print(f"目标服务器: {args.host}:{args.port}")
    print("按 Ctrl+C 停止")
    print("=" * 50)
    print()

    client = ReverseProxyClient(args.host, args.port, args.dll)

    try:
        asyncio.run(client.connect_and_serve())
    except KeyboardInterrupt:
        print("\n正在停止...")
        client.stop()


if __name__ == '__main__':
    main()
