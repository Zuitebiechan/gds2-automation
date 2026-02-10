"""
VCI Proxy 网络协议

消息格式:
┌──────────┬──────────┬──────────┬──────────┐
│  Magic   │  Length  │  MsgType │ Sequence │
│  4 bytes │  4 bytes │  2 bytes │  4 bytes │
├──────────┴──────────┴──────────┴──────────┤
│              Message Body                  │
│            (Variable Length)               │
└────────────────────────────────────────────┘
"""

import struct
from dataclasses import dataclass
from typing import Tuple, Optional, List
from enum import IntEnum


# 协议常量
MAGIC = 0x4A325334  # "J254" in ASCII
HEADER_SIZE = 14


class MsgType(IntEnum):
    """消息类型"""
    # 请求 (0x00xx)
    OPEN_REQ = 0x0001
    CLOSE_REQ = 0x0002
    CONNECT_REQ = 0x0003
    DISCONNECT_REQ = 0x0004
    READ_MSGS_REQ = 0x0005
    WRITE_MSGS_REQ = 0x0006
    IOCTL_REQ = 0x0007
    START_FILTER_REQ = 0x0010
    STOP_FILTER_REQ = 0x0011
    READ_VERSION_REQ = 0x0020
    GET_LAST_ERROR_REQ = 0x0021
    AUTH_REQ = 0x00FE
    HEARTBEAT = 0x00FF

    # 响应 (0x80xx)
    OPEN_RSP = 0x8001
    CLOSE_RSP = 0x8002
    CONNECT_RSP = 0x8003
    DISCONNECT_RSP = 0x8004
    READ_MSGS_RSP = 0x8005
    WRITE_MSGS_RSP = 0x8006
    IOCTL_RSP = 0x8007
    START_FILTER_RSP = 0x8010
    STOP_FILTER_RSP = 0x8011
    READ_VERSION_RSP = 0x8020
    GET_LAST_ERROR_RSP = 0x8021
    AUTH_RSP = 0x80FE
    HEARTBEAT_ACK = 0x80FF


@dataclass
class Message:
    """协议消息"""
    msg_type: int
    sequence: int
    body: bytes

    def encode(self) -> bytes:
        """编码为字节流"""
        length = HEADER_SIZE + len(self.body)
        # Format: Magic(4) + Length(4) + MsgType(2) + Sequence(4) = 14 bytes
        header = struct.pack('>IIHI',
                            MAGIC,
                            length,
                            self.msg_type,
                            self.sequence)
        return header + self.body

    @classmethod
    def decode_header(cls, data: bytes) -> Tuple[int, int, int, int]:
        """解码消息头，返回 (magic, length, msg_type, sequence)"""
        if len(data) < HEADER_SIZE:
            raise ValueError(f"Header too short: {len(data)} < {HEADER_SIZE}")
        return struct.unpack('>IIHI', data[:HEADER_SIZE])


class ProtocolEncoder:
    """协议编码器"""

    @staticmethod
    def encode_open_req(device_name: Optional[str] = None, sequence: int = 0) -> bytes:
        """编码 PassThruOpen 请求"""
        body = device_name.encode('utf-8') + b'\x00' if device_name else b''
        return Message(MsgType.OPEN_REQ, sequence, body).encode()

    @staticmethod
    def encode_open_rsp(return_code: int, device_id: int, sequence: int = 0) -> bytes:
        """编码 PassThruOpen 响应"""
        body = struct.pack('>II', return_code, device_id)
        return Message(MsgType.OPEN_RSP, sequence, body).encode()

    @staticmethod
    def encode_close_req(device_id: int, sequence: int = 0) -> bytes:
        """编码 PassThruClose 请求"""
        body = struct.pack('>I', device_id)
        return Message(MsgType.CLOSE_REQ, sequence, body).encode()

    @staticmethod
    def encode_close_rsp(return_code: int, sequence: int = 0) -> bytes:
        """编码 PassThruClose 响应"""
        body = struct.pack('>I', return_code)
        return Message(MsgType.CLOSE_RSP, sequence, body).encode()

    @staticmethod
    def encode_connect_req(device_id: int, protocol_id: int, flags: int,
                          baudrate: int, sequence: int = 0) -> bytes:
        """编码 PassThruConnect 请求"""
        body = struct.pack('>IIII', device_id, protocol_id, flags, baudrate)
        return Message(MsgType.CONNECT_REQ, sequence, body).encode()

    @staticmethod
    def encode_connect_rsp(return_code: int, channel_id: int, sequence: int = 0) -> bytes:
        """编码 PassThruConnect 响应"""
        body = struct.pack('>II', return_code, channel_id)
        return Message(MsgType.CONNECT_RSP, sequence, body).encode()

    @staticmethod
    def encode_disconnect_req(channel_id: int, sequence: int = 0) -> bytes:
        """编码 PassThruDisconnect 请求"""
        body = struct.pack('>I', channel_id)
        return Message(MsgType.DISCONNECT_REQ, sequence, body).encode()

    @staticmethod
    def encode_disconnect_rsp(return_code: int, sequence: int = 0) -> bytes:
        """编码 PassThruDisconnect 响应"""
        body = struct.pack('>I', return_code)
        return Message(MsgType.DISCONNECT_RSP, sequence, body).encode()

    @staticmethod
    def encode_read_msgs_req(channel_id: int, num_msgs: int, timeout: int,
                            sequence: int = 0) -> bytes:
        """编码 PassThruReadMsgs 请求"""
        body = struct.pack('>III', channel_id, num_msgs, timeout)
        return Message(MsgType.READ_MSGS_REQ, sequence, body).encode()

    @staticmethod
    def encode_read_msgs_rsp(return_code: int, messages: List[dict],
                            sequence: int = 0) -> bytes:
        """
        编码 PassThruReadMsgs 响应

        messages: List of dict with keys:
            - protocol_id, rx_status, tx_flags, timestamp, data (bytes)
        """
        body = struct.pack('>II', return_code, len(messages))
        for msg in messages:
            data = msg.get('data', b'')
            body += struct.pack('>IIIII',
                               msg.get('protocol_id', 0),
                               msg.get('rx_status', 0),
                               msg.get('tx_flags', 0),
                               msg.get('timestamp', 0),
                               len(data))
            body += data
        return Message(MsgType.READ_MSGS_RSP, sequence, body).encode()

    @staticmethod
    def encode_write_msgs_req(channel_id: int, messages: List[dict],
                             timeout: int, sequence: int = 0) -> bytes:
        """
        编码 PassThruWriteMsgs 请求

        messages: List of dict with keys:
            - protocol_id, tx_flags, data (bytes)
        """
        body = struct.pack('>III', channel_id, len(messages), timeout)
        for msg in messages:
            data = msg.get('data', b'')
            body += struct.pack('>IIIII',
                               msg.get('protocol_id', 0),
                               msg.get('rx_status', 0),
                               msg.get('tx_flags', 0),
                               msg.get('timestamp', 0),
                               len(data))
            body += data
        return Message(MsgType.WRITE_MSGS_REQ, sequence, body).encode()

    @staticmethod
    def encode_write_msgs_rsp(return_code: int, num_written: int,
                             sequence: int = 0) -> bytes:
        """编码 PassThruWriteMsgs 响应"""
        body = struct.pack('>II', return_code, num_written)
        return Message(MsgType.WRITE_MSGS_RSP, sequence, body).encode()

    @staticmethod
    def encode_read_version_req(device_id: int, sequence: int = 0) -> bytes:
        """编码 PassThruReadVersion 请求"""
        body = struct.pack('>I', device_id)
        return Message(MsgType.READ_VERSION_REQ, sequence, body).encode()

    @staticmethod
    def encode_read_version_rsp(return_code: int, firmware_ver: str,
                               dll_ver: str, api_ver: str,
                               sequence: int = 0) -> bytes:
        """编码 PassThruReadVersion 响应"""
        fw_bytes = firmware_ver.encode('utf-8')[:80].ljust(80, b'\x00')
        dll_bytes = dll_ver.encode('utf-8')[:80].ljust(80, b'\x00')
        api_bytes = api_ver.encode('utf-8')[:80].ljust(80, b'\x00')
        body = struct.pack('>I', return_code) + fw_bytes + dll_bytes + api_bytes
        return Message(MsgType.READ_VERSION_RSP, sequence, body).encode()

    @staticmethod
    def encode_heartbeat(sequence: int = 0) -> bytes:
        """编码心跳请求"""
        return Message(MsgType.HEARTBEAT, sequence, b'').encode()

    @staticmethod
    def encode_heartbeat_ack(sequence: int = 0) -> bytes:
        """编码心跳响应"""
        return Message(MsgType.HEARTBEAT_ACK, sequence, b'').encode()

    @staticmethod
    def encode_auth_req(timestamp: int, signature: bytes,
                        sequence: int = 0) -> bytes:
        """编码认证请求: 8-byte timestamp + 32-byte HMAC-SHA256"""
        body = struct.pack('>Q', timestamp) + signature
        return Message(MsgType.AUTH_REQ, sequence, body).encode()

    @staticmethod
    def encode_auth_rsp(success: bool, message: str = "",
                        sequence: int = 0) -> bytes:
        """编码认证响应: 1-byte success flag + UTF-8 message"""
        msg_bytes = message.encode('utf-8')
        body = struct.pack('>B', 1 if success else 0) + msg_bytes
        return Message(MsgType.AUTH_RSP, sequence, body).encode()

    @staticmethod
    def encode_start_filter_req(channel_id: int, filter_type: int,
                                mask_msg: Optional[dict], pattern_msg: Optional[dict],
                                flow_control_msg: Optional[dict],
                                sequence: int = 0) -> bytes:
        """编码 PassThruStartMsgFilter 请求"""
        body = struct.pack('>II', channel_id, filter_type)

        # 编码每个可选的消息 (标志 + 消息内容)
        for msg in [mask_msg, pattern_msg, flow_control_msg]:
            if msg is not None:
                data = msg.get('data', b'')
                body += struct.pack('>B', 1)  # present flag
                body += struct.pack('>IIIII',
                                   msg.get('protocol_id', 0),
                                   msg.get('rx_status', 0),
                                   msg.get('tx_flags', 0),
                                   msg.get('timestamp', 0),
                                   len(data))
                body += data
            else:
                body += struct.pack('>B', 0)  # not present

        return Message(MsgType.START_FILTER_REQ, sequence, body).encode()

    @staticmethod
    def encode_start_filter_rsp(return_code: int, filter_id: int,
                                sequence: int = 0) -> bytes:
        """编码 PassThruStartMsgFilter 响应"""
        body = struct.pack('>II', return_code, filter_id)
        return Message(MsgType.START_FILTER_RSP, sequence, body).encode()

    @staticmethod
    def encode_stop_filter_req(channel_id: int, filter_id: int,
                               sequence: int = 0) -> bytes:
        """编码 PassThruStopMsgFilter 请求"""
        body = struct.pack('>II', channel_id, filter_id)
        return Message(MsgType.STOP_FILTER_REQ, sequence, body).encode()

    @staticmethod
    def encode_stop_filter_rsp(return_code: int, sequence: int = 0) -> bytes:
        """编码 PassThruStopMsgFilter 响应"""
        body = struct.pack('>I', return_code)
        return Message(MsgType.STOP_FILTER_RSP, sequence, body).encode()

    @staticmethod
    def encode_ioctl_req(channel_id: int, ioctl_id: int,
                        input_data: Optional[bytes] = None,
                        sequence: int = 0) -> bytes:
        """编码 PassThruIoctl 请求"""
        input_bytes = input_data if input_data else b''
        body = struct.pack('>III', channel_id, ioctl_id, len(input_bytes))
        body += input_bytes
        return Message(MsgType.IOCTL_REQ, sequence, body).encode()

    @staticmethod
    def encode_ioctl_rsp(return_code: int, output_data: Optional[bytes] = None,
                        sequence: int = 0) -> bytes:
        """编码 PassThruIoctl 响应"""
        output_bytes = output_data if output_data else b''
        body = struct.pack('>II', return_code, len(output_bytes))
        body += output_bytes
        return Message(MsgType.IOCTL_RSP, sequence, body).encode()


class ProtocolDecoder:
    """协议解码器"""

    @staticmethod
    def decode_open_req(body: bytes) -> Optional[str]:
        """解码 PassThruOpen 请求，返回 device_name"""
        if not body:
            return None
        return body.rstrip(b'\x00').decode('utf-8', errors='replace')

    @staticmethod
    def decode_open_rsp(body: bytes) -> Tuple[int, int]:
        """解码 PassThruOpen 响应，返回 (return_code, device_id)"""
        return struct.unpack('>II', body[:8])

    @staticmethod
    def decode_close_req(body: bytes) -> int:
        """解码 PassThruClose 请求，返回 device_id"""
        return struct.unpack('>I', body[:4])[0]

    @staticmethod
    def decode_close_rsp(body: bytes) -> int:
        """解码 PassThruClose 响应，返回 return_code"""
        return struct.unpack('>I', body[:4])[0]

    @staticmethod
    def decode_connect_req(body: bytes) -> Tuple[int, int, int, int]:
        """解码 PassThruConnect 请求，返回 (device_id, protocol_id, flags, baudrate)"""
        return struct.unpack('>IIII', body[:16])

    @staticmethod
    def decode_connect_rsp(body: bytes) -> Tuple[int, int]:
        """解码 PassThruConnect 响应，返回 (return_code, channel_id)"""
        return struct.unpack('>II', body[:8])

    @staticmethod
    def decode_disconnect_req(body: bytes) -> int:
        """解码 PassThruDisconnect 请求，返回 channel_id"""
        return struct.unpack('>I', body[:4])[0]

    @staticmethod
    def decode_disconnect_rsp(body: bytes) -> int:
        """解码 PassThruDisconnect 响应，返回 return_code"""
        return struct.unpack('>I', body[:4])[0]

    @staticmethod
    def decode_read_msgs_req(body: bytes) -> Tuple[int, int, int]:
        """解码 PassThruReadMsgs 请求，返回 (channel_id, num_msgs, timeout)"""
        return struct.unpack('>III', body[:12])

    @staticmethod
    def decode_read_msgs_rsp(body: bytes) -> Tuple[int, List[dict]]:
        """
        解码 PassThruReadMsgs 响应

        返回: (return_code, messages)
        messages: List of dict with keys:
            - protocol_id, rx_status, tx_flags, timestamp, data (bytes)
        """
        return_code, num_msgs = struct.unpack('>II', body[:8])
        messages = []
        offset = 8

        for _ in range(num_msgs):
            protocol_id, rx_status, tx_flags, timestamp, data_size = \
                struct.unpack('>IIIII', body[offset:offset+20])
            offset += 20
            data = body[offset:offset+data_size]
            offset += data_size

            messages.append({
                'protocol_id': protocol_id,
                'rx_status': rx_status,
                'tx_flags': tx_flags,
                'timestamp': timestamp,
                'data': data
            })

        return return_code, messages

    @staticmethod
    def decode_write_msgs_req(body: bytes) -> Tuple[int, List[dict], int]:
        """
        解码 PassThruWriteMsgs 请求

        返回: (channel_id, messages, timeout)
        """
        channel_id, num_msgs, timeout = struct.unpack('>III', body[:12])
        messages = []
        offset = 12

        for _ in range(num_msgs):
            protocol_id, rx_status, tx_flags, timestamp, data_size = \
                struct.unpack('>IIIII', body[offset:offset+20])
            offset += 20
            data = body[offset:offset+data_size]
            offset += data_size

            messages.append({
                'protocol_id': protocol_id,
                'rx_status': rx_status,
                'tx_flags': tx_flags,
                'timestamp': timestamp,
                'data': data
            })

        return channel_id, messages, timeout

    @staticmethod
    def decode_write_msgs_rsp(body: bytes) -> Tuple[int, int]:
        """解码 PassThruWriteMsgs 响应，返回 (return_code, num_written)"""
        return struct.unpack('>II', body[:8])

    @staticmethod
    def decode_read_version_req(body: bytes) -> int:
        """解码 PassThruReadVersion 请求，返回 device_id"""
        return struct.unpack('>I', body[:4])[0]

    @staticmethod
    def decode_read_version_rsp(body: bytes) -> Tuple[int, str, str, str]:
        """解码 PassThruReadVersion 响应，返回 (return_code, fw_ver, dll_ver, api_ver)"""
        return_code = struct.unpack('>I', body[:4])[0]
        fw_ver = body[4:84].rstrip(b'\x00').decode('utf-8', errors='replace')
        dll_ver = body[84:164].rstrip(b'\x00').decode('utf-8', errors='replace')
        api_ver = body[164:244].rstrip(b'\x00').decode('utf-8', errors='replace')
        return return_code, fw_ver, dll_ver, api_ver

    @staticmethod
    def decode_start_filter_req(body: bytes) -> Tuple[int, int, Optional[dict],
                                                       Optional[dict], Optional[dict]]:
        """
        解码 PassThruStartMsgFilter 请求

        返回: (channel_id, filter_type, mask_msg, pattern_msg, flow_control_msg)
        """
        channel_id, filter_type = struct.unpack('>II', body[:8])
        offset = 8

        messages = []
        for _ in range(3):  # mask, pattern, flow_control
            present = body[offset]
            offset += 1
            if present:
                protocol_id, rx_status, tx_flags, timestamp, data_size = \
                    struct.unpack('>IIIII', body[offset:offset+20])
                offset += 20
                data = body[offset:offset+data_size]
                offset += data_size
                messages.append({
                    'protocol_id': protocol_id,
                    'rx_status': rx_status,
                    'tx_flags': tx_flags,
                    'timestamp': timestamp,
                    'data': data
                })
            else:
                messages.append(None)

        return channel_id, filter_type, messages[0], messages[1], messages[2]

    @staticmethod
    def decode_start_filter_rsp(body: bytes) -> Tuple[int, int]:
        """解码 PassThruStartMsgFilter 响应，返回 (return_code, filter_id)"""
        return struct.unpack('>II', body[:8])

    @staticmethod
    def decode_stop_filter_req(body: bytes) -> Tuple[int, int]:
        """解码 PassThruStopMsgFilter 请求，返回 (channel_id, filter_id)"""
        return struct.unpack('>II', body[:8])

    @staticmethod
    def decode_stop_filter_rsp(body: bytes) -> int:
        """解码 PassThruStopMsgFilter 响应，返回 return_code"""
        return struct.unpack('>I', body[:4])[0]

    @staticmethod
    def decode_ioctl_req(body: bytes) -> Tuple[int, int, Optional[bytes]]:
        """解码 PassThruIoctl 请求，返回 (channel_id, ioctl_id, input_data)"""
        channel_id, ioctl_id, input_len = struct.unpack('>III', body[:12])
        input_data = body[12:12+input_len] if input_len > 0 else None
        return channel_id, ioctl_id, input_data

    @staticmethod
    def decode_ioctl_rsp(body: bytes) -> Tuple[int, Optional[bytes]]:
        """解码 PassThruIoctl 响应，返回 (return_code, output_data)"""
        return_code, output_len = struct.unpack('>II', body[:8])
        output_data = body[8:8+output_len] if output_len > 0 else None
        return return_code, output_data

    @staticmethod
    def decode_auth_req(body: bytes) -> Tuple[int, bytes]:
        """解码认证请求，返回 (timestamp, signature_32bytes)"""
        timestamp = struct.unpack('>Q', body[:8])[0]
        signature = body[8:40]
        return timestamp, signature

    @staticmethod
    def decode_auth_rsp(body: bytes) -> Tuple[bool, str]:
        """解码认证响应，返回 (success, message)"""
        success = body[0] != 0
        message = body[1:].decode('utf-8', errors='replace') if len(body) > 1 else ""
        return success, message
