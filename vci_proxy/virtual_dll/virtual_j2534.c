/**
 * Virtual J2534 DLL
 *
 * Implements J2534 API by forwarding calls over TCP to VCI Proxy server.
 *
 * Build (MSVC 32-bit):
 *   cl /LD /D BUILDING_DLL virtual_j2534.c /link ws2_32.lib /out:virtual_j2534.dll
 *
 * Build (MinGW 32-bit):
 *   i686-w64-mingw32-gcc -shared -o virtual_j2534.dll virtual_j2534.c -lws2_32
 */

// IMPORTANT: Include winsock2.h BEFORE windows.h to avoid conflicts
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>

#define BUILDING_DLL
#include "j2534.h"
#include <stdio.h>
#include <string.h>

#pragma comment(lib, "ws2_32.lib")

// Protocol constants (must match Python protocol.py)
#define MAGIC           0x4A325334
#define HEADER_SIZE     14

// Message types
#define MSG_OPEN_REQ            0x0001
#define MSG_OPEN_RSP            0x8001
#define MSG_CLOSE_REQ           0x0002
#define MSG_CLOSE_RSP           0x8002
#define MSG_CONNECT_REQ         0x0003
#define MSG_CONNECT_RSP         0x8003
#define MSG_DISCONNECT_REQ      0x0004
#define MSG_DISCONNECT_RSP      0x8004
#define MSG_READ_MSGS_REQ       0x0005
#define MSG_READ_MSGS_RSP       0x8005
#define MSG_WRITE_MSGS_REQ      0x0006
#define MSG_WRITE_MSGS_RSP      0x8006
#define MSG_IOCTL_REQ           0x0007
#define MSG_IOCTL_RSP           0x8007
#define MSG_START_FILTER_REQ    0x0010
#define MSG_START_FILTER_RSP    0x8010
#define MSG_STOP_FILTER_REQ     0x0011
#define MSG_STOP_FILTER_RSP     0x8011
#define MSG_READ_VERSION_REQ    0x0020
#define MSG_READ_VERSION_RSP    0x8020
#define MSG_HEARTBEAT           0x00FF
#define MSG_HEARTBEAT_ACK       0x80FF

// Server configuration
static const char* SERVER_HOST = "127.0.0.1";
static const int SERVER_PORT = 9001;

// Global state
static SOCKET g_socket = INVALID_SOCKET;
static BOOL g_initialized = FALSE;
static unsigned long g_sequence = 0;
static char g_last_error[256] = {0};
static CRITICAL_SECTION g_cs;

// Helper: Convert to big-endian (network byte order)
static void write_uint32_be(unsigned char* buf, unsigned long val) {
    buf[0] = (val >> 24) & 0xFF;
    buf[1] = (val >> 16) & 0xFF;
    buf[2] = (val >> 8) & 0xFF;
    buf[3] = val & 0xFF;
}

static void write_uint16_be(unsigned char* buf, unsigned short val) {
    buf[0] = (val >> 8) & 0xFF;
    buf[1] = val & 0xFF;
}

static unsigned long read_uint32_be(const unsigned char* buf) {
    return ((unsigned long)buf[0] << 24) |
           ((unsigned long)buf[1] << 16) |
           ((unsigned long)buf[2] << 8) |
           (unsigned long)buf[3];
}

static unsigned short read_uint16_be(const unsigned char* buf) {
    return ((unsigned short)buf[0] << 8) | (unsigned short)buf[1];
}

// Initialize Winsock and connect to server
static BOOL connect_to_server(void) {
    WSADATA wsaData;
    struct sockaddr_in serverAddr;

    if (g_socket != INVALID_SOCKET) {
        return TRUE;  // Already connected
    }

    // Initialize Winsock
    if (WSAStartup(MAKEWORD(2, 2), &wsaData) != 0) {
        sprintf_s(g_last_error, sizeof(g_last_error), "WSAStartup failed");
        return FALSE;
    }

    // Create socket
    g_socket = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (g_socket == INVALID_SOCKET) {
        sprintf_s(g_last_error, sizeof(g_last_error), "Socket creation failed");
        WSACleanup();
        return FALSE;
    }

    // Connect to server
    memset(&serverAddr, 0, sizeof(serverAddr));
    serverAddr.sin_family = AF_INET;
    serverAddr.sin_port = htons(SERVER_PORT);
    inet_pton(AF_INET, SERVER_HOST, &serverAddr.sin_addr);

    if (connect(g_socket, (struct sockaddr*)&serverAddr, sizeof(serverAddr)) == SOCKET_ERROR) {
        sprintf_s(g_last_error, sizeof(g_last_error),
                  "Connection to %s:%d failed", SERVER_HOST, SERVER_PORT);
        closesocket(g_socket);
        g_socket = INVALID_SOCKET;
        WSACleanup();
        return FALSE;
    }

    return TRUE;
}

// Send message and receive response
static long send_recv(unsigned short msg_type, const unsigned char* body,
                      unsigned long body_len, unsigned char* resp_body,
                      unsigned long* resp_len, unsigned long max_resp_len) {
    unsigned char header[HEADER_SIZE];
    unsigned char resp_header[HEADER_SIZE];
    unsigned long total_len = HEADER_SIZE + body_len;
    unsigned long magic, length, resp_type, sequence;
    int sent, received;

    EnterCriticalSection(&g_cs);

    if (!connect_to_server()) {
        LeaveCriticalSection(&g_cs);
        return ERR_DEVICE_NOT_CONNECTED;
    }

    g_sequence++;

    // Build header
    write_uint32_be(header, MAGIC);
    write_uint32_be(header + 4, total_len);
    write_uint16_be(header + 8, msg_type);
    write_uint32_be(header + 10, g_sequence);

    // Send header
    sent = send(g_socket, (const char*)header, HEADER_SIZE, 0);
    if (sent != HEADER_SIZE) {
        closesocket(g_socket);
        g_socket = INVALID_SOCKET;
        LeaveCriticalSection(&g_cs);
        return ERR_FAILED;
    }

    // Send body
    if (body_len > 0) {
        sent = send(g_socket, (const char*)body, body_len, 0);
        if (sent != (int)body_len) {
            closesocket(g_socket);
            g_socket = INVALID_SOCKET;
            LeaveCriticalSection(&g_cs);
            return ERR_FAILED;
        }
    }

    // Receive response header
    received = recv(g_socket, (char*)resp_header, HEADER_SIZE, MSG_WAITALL);
    if (received != HEADER_SIZE) {
        closesocket(g_socket);
        g_socket = INVALID_SOCKET;
        LeaveCriticalSection(&g_cs);
        return ERR_FAILED;
    }

    // Parse response header
    magic = read_uint32_be(resp_header);
    length = read_uint32_be(resp_header + 4);
    resp_type = read_uint16_be(resp_header + 8);
    sequence = read_uint32_be(resp_header + 10);

    if (magic != MAGIC) {
        LeaveCriticalSection(&g_cs);
        return ERR_FAILED;
    }

    // Receive response body
    *resp_len = length - HEADER_SIZE;
    if (*resp_len > 0) {
        if (*resp_len > max_resp_len) {
            LeaveCriticalSection(&g_cs);
            return ERR_BUFFER_OVERFLOW;
        }
        received = recv(g_socket, (char*)resp_body, *resp_len, MSG_WAITALL);
        if (received != (int)*resp_len) {
            closesocket(g_socket);
            g_socket = INVALID_SOCKET;
            LeaveCriticalSection(&g_cs);
            return ERR_FAILED;
        }
    }

    LeaveCriticalSection(&g_cs);
    return STATUS_NOERROR;
}

// DLL Entry Point
BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpvReserved) {
    switch (fdwReason) {
        case DLL_PROCESS_ATTACH:
            InitializeCriticalSection(&g_cs);
            g_initialized = TRUE;
            break;
        case DLL_PROCESS_DETACH:
            if (g_socket != INVALID_SOCKET) {
                closesocket(g_socket);
                WSACleanup();
            }
            DeleteCriticalSection(&g_cs);
            break;
    }
    return TRUE;
}

// ============================================================================
// J2534 API Implementation
// ============================================================================

J2534_API long __stdcall PassThruOpen(void* pName, unsigned long* pDeviceID) {
    unsigned char body[256] = {0};
    unsigned char resp[64];
    unsigned long resp_len;
    unsigned long body_len = 0;
    long ret;

    if (pDeviceID == NULL) {
        return ERR_NULL_PARAMETER;
    }

    // Encode device name (optional)
    if (pName != NULL) {
        const char* name = (const char*)pName;
        size_t name_len = strlen(name);
        if (name_len > 0) {
            memcpy(body, name, name_len);
            body[name_len] = 0;
            body_len = (unsigned long)(name_len + 1);
        }
    }

    ret = send_recv(MSG_OPEN_REQ, body, body_len, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        return ret;
    }

    if (resp_len >= 8) {
        unsigned long return_code = read_uint32_be(resp);
        *pDeviceID = read_uint32_be(resp + 4);
        return return_code;
    }

    return ERR_FAILED;
}

J2534_API long __stdcall PassThruClose(unsigned long DeviceID) {
    unsigned char body[4];
    unsigned char resp[16];
    unsigned long resp_len;
    long ret;

    write_uint32_be(body, DeviceID);

    ret = send_recv(MSG_CLOSE_REQ, body, 4, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        return ret;
    }

    if (resp_len >= 4) {
        return read_uint32_be(resp);
    }

    return ERR_FAILED;
}

J2534_API long __stdcall PassThruConnect(unsigned long DeviceID,
                                          unsigned long ProtocolID,
                                          unsigned long Flags,
                                          unsigned long BaudRate,
                                          unsigned long* pChannelID) {
    unsigned char body[16];
    unsigned char resp[16];
    unsigned long resp_len;
    long ret;

    if (pChannelID == NULL) {
        return ERR_NULL_PARAMETER;
    }

    write_uint32_be(body, DeviceID);
    write_uint32_be(body + 4, ProtocolID);
    write_uint32_be(body + 8, Flags);
    write_uint32_be(body + 12, BaudRate);

    ret = send_recv(MSG_CONNECT_REQ, body, 16, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        return ret;
    }

    if (resp_len >= 8) {
        unsigned long return_code = read_uint32_be(resp);
        *pChannelID = read_uint32_be(resp + 4);
        return return_code;
    }

    return ERR_FAILED;
}

J2534_API long __stdcall PassThruDisconnect(unsigned long ChannelID) {
    unsigned char body[4];
    unsigned char resp[16];
    unsigned long resp_len;
    long ret;

    write_uint32_be(body, ChannelID);

    ret = send_recv(MSG_DISCONNECT_REQ, body, 4, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        return ret;
    }

    if (resp_len >= 4) {
        return read_uint32_be(resp);
    }

    return ERR_FAILED;
}

J2534_API long __stdcall PassThruReadMsgs(unsigned long ChannelID,
                                           PASSTHRU_MSG* pMsg,
                                           unsigned long* pNumMsgs,
                                           unsigned long Timeout) {
    unsigned char body[12];
    unsigned char resp[8192];
    unsigned long resp_len;
    long ret;
    unsigned long return_code, num_msgs;
    unsigned long offset, i;

    if (pMsg == NULL || pNumMsgs == NULL) {
        return ERR_NULL_PARAMETER;
    }

    write_uint32_be(body, ChannelID);
    write_uint32_be(body + 4, *pNumMsgs);
    write_uint32_be(body + 8, Timeout);

    ret = send_recv(MSG_READ_MSGS_REQ, body, 12, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        return ret;
    }

    if (resp_len < 8) {
        return ERR_FAILED;
    }

    return_code = read_uint32_be(resp);
    num_msgs = read_uint32_be(resp + 4);

    offset = 8;
    for (i = 0; i < num_msgs && i < *pNumMsgs; i++) {
        if (offset + 20 > resp_len) break;

        pMsg[i].ProtocolID = read_uint32_be(resp + offset);
        pMsg[i].RxStatus = read_uint32_be(resp + offset + 4);
        pMsg[i].TxFlags = read_uint32_be(resp + offset + 8);
        pMsg[i].Timestamp = read_uint32_be(resp + offset + 12);
        pMsg[i].DataSize = read_uint32_be(resp + offset + 16);
        offset += 20;

        if (offset + pMsg[i].DataSize > resp_len) break;
        if (pMsg[i].DataSize > sizeof(pMsg[i].Data)) {
            pMsg[i].DataSize = sizeof(pMsg[i].Data);
        }
        memcpy(pMsg[i].Data, resp + offset, pMsg[i].DataSize);
        offset += pMsg[i].DataSize;
    }

    *pNumMsgs = i;
    return return_code;
}

J2534_API long __stdcall PassThruWriteMsgs(unsigned long ChannelID,
                                            PASSTHRU_MSG* pMsg,
                                            unsigned long* pNumMsgs,
                                            unsigned long Timeout) {
    unsigned char body[8192];
    unsigned char resp[16];
    unsigned long resp_len;
    unsigned long body_len;
    unsigned long i;
    long ret;

    if (pMsg == NULL || pNumMsgs == NULL) {
        return ERR_NULL_PARAMETER;
    }

    write_uint32_be(body, ChannelID);
    write_uint32_be(body + 4, *pNumMsgs);
    write_uint32_be(body + 8, Timeout);
    body_len = 12;

    for (i = 0; i < *pNumMsgs; i++) {
        write_uint32_be(body + body_len, pMsg[i].ProtocolID);
        write_uint32_be(body + body_len + 4, pMsg[i].RxStatus);
        write_uint32_be(body + body_len + 8, pMsg[i].TxFlags);
        write_uint32_be(body + body_len + 12, pMsg[i].Timestamp);
        write_uint32_be(body + body_len + 16, pMsg[i].DataSize);
        body_len += 20;

        if (body_len + pMsg[i].DataSize > sizeof(body)) break;
        memcpy(body + body_len, pMsg[i].Data, pMsg[i].DataSize);
        body_len += pMsg[i].DataSize;
    }

    ret = send_recv(MSG_WRITE_MSGS_REQ, body, body_len, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        return ret;
    }

    if (resp_len >= 8) {
        unsigned long return_code = read_uint32_be(resp);
        *pNumMsgs = read_uint32_be(resp + 4);
        return return_code;
    }

    return ERR_FAILED;
}

J2534_API long __stdcall PassThruStartPeriodicMsg(unsigned long ChannelID,
                                                   PASSTHRU_MSG* pMsg,
                                                   unsigned long* pMsgID,
                                                   unsigned long TimeInterval) {
    // TODO: Implement
    return ERR_NOT_SUPPORTED;
}

J2534_API long __stdcall PassThruStopPeriodicMsg(unsigned long ChannelID,
                                                  unsigned long MsgID) {
    // TODO: Implement
    return ERR_NOT_SUPPORTED;
}

J2534_API long __stdcall PassThruStartMsgFilter(unsigned long ChannelID,
                                                 unsigned long FilterType,
                                                 PASSTHRU_MSG* pMaskMsg,
                                                 PASSTHRU_MSG* pPatternMsg,
                                                 PASSTHRU_MSG* pFlowControlMsg,
                                                 unsigned long* pFilterID) {
    unsigned char body[8192];
    unsigned char resp[16];
    unsigned long resp_len;
    unsigned long body_len = 0;
    long ret;

    if (pFilterID == NULL) {
        return ERR_NULL_PARAMETER;
    }

    // Header: ChannelID + FilterType
    write_uint32_be(body, ChannelID);
    write_uint32_be(body + 4, FilterType);
    body_len = 8;

    // Encode MaskMsg (present flag + message)
    if (pMaskMsg != NULL) {
        body[body_len++] = 1;  // present
        write_uint32_be(body + body_len, pMaskMsg->ProtocolID);
        write_uint32_be(body + body_len + 4, pMaskMsg->RxStatus);
        write_uint32_be(body + body_len + 8, pMaskMsg->TxFlags);
        write_uint32_be(body + body_len + 12, pMaskMsg->Timestamp);
        write_uint32_be(body + body_len + 16, pMaskMsg->DataSize);
        body_len += 20;
        memcpy(body + body_len, pMaskMsg->Data, pMaskMsg->DataSize);
        body_len += pMaskMsg->DataSize;
    } else {
        body[body_len++] = 0;  // not present
    }

    // Encode PatternMsg
    if (pPatternMsg != NULL) {
        body[body_len++] = 1;
        write_uint32_be(body + body_len, pPatternMsg->ProtocolID);
        write_uint32_be(body + body_len + 4, pPatternMsg->RxStatus);
        write_uint32_be(body + body_len + 8, pPatternMsg->TxFlags);
        write_uint32_be(body + body_len + 12, pPatternMsg->Timestamp);
        write_uint32_be(body + body_len + 16, pPatternMsg->DataSize);
        body_len += 20;
        memcpy(body + body_len, pPatternMsg->Data, pPatternMsg->DataSize);
        body_len += pPatternMsg->DataSize;
    } else {
        body[body_len++] = 0;
    }

    // Encode FlowControlMsg
    if (pFlowControlMsg != NULL) {
        body[body_len++] = 1;
        write_uint32_be(body + body_len, pFlowControlMsg->ProtocolID);
        write_uint32_be(body + body_len + 4, pFlowControlMsg->RxStatus);
        write_uint32_be(body + body_len + 8, pFlowControlMsg->TxFlags);
        write_uint32_be(body + body_len + 12, pFlowControlMsg->Timestamp);
        write_uint32_be(body + body_len + 16, pFlowControlMsg->DataSize);
        body_len += 20;
        memcpy(body + body_len, pFlowControlMsg->Data, pFlowControlMsg->DataSize);
        body_len += pFlowControlMsg->DataSize;
    } else {
        body[body_len++] = 0;
    }

    ret = send_recv(MSG_START_FILTER_REQ, body, body_len, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        return ret;
    }

    if (resp_len >= 8) {
        unsigned long return_code = read_uint32_be(resp);
        *pFilterID = read_uint32_be(resp + 4);
        return return_code;
    }

    return ERR_FAILED;
}

J2534_API long __stdcall PassThruStopMsgFilter(unsigned long ChannelID,
                                                unsigned long FilterID) {
    unsigned char body[8];
    unsigned char resp[16];
    unsigned long resp_len;
    long ret;

    write_uint32_be(body, ChannelID);
    write_uint32_be(body + 4, FilterID);

    ret = send_recv(MSG_STOP_FILTER_REQ, body, 8, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        return ret;
    }

    if (resp_len >= 4) {
        return read_uint32_be(resp);
    }

    return ERR_FAILED;
}

J2534_API long __stdcall PassThruSetProgrammingVoltage(unsigned long DeviceID,
                                                        unsigned long PinNumber,
                                                        unsigned long Voltage) {
    // TODO: Implement
    return ERR_NOT_SUPPORTED;
}

J2534_API long __stdcall PassThruReadVersion(unsigned long DeviceID,
                                              char* pFirmwareVersion,
                                              char* pDllVersion,
                                              char* pApiVersion) {
    unsigned char body[4];
    unsigned char resp[256];
    unsigned long resp_len;
    long ret;

    if (pFirmwareVersion == NULL || pDllVersion == NULL || pApiVersion == NULL) {
        return ERR_NULL_PARAMETER;
    }

    write_uint32_be(body, DeviceID);

    ret = send_recv(MSG_READ_VERSION_REQ, body, 4, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        return ret;
    }

    if (resp_len >= 244) {  // 4 + 80 + 80 + 80
        unsigned long return_code = read_uint32_be(resp);
        memcpy(pFirmwareVersion, resp + 4, 80);
        pFirmwareVersion[79] = 0;
        memcpy(pDllVersion, resp + 84, 80);
        pDllVersion[79] = 0;
        memcpy(pApiVersion, resp + 164, 80);
        pApiVersion[79] = 0;
        return return_code;
    }

    return ERR_FAILED;
}

J2534_API long __stdcall PassThruGetLastError(char* pErrorDescription) {
    if (pErrorDescription == NULL) {
        return ERR_NULL_PARAMETER;
    }

    strcpy_s(pErrorDescription, 80, g_last_error);
    return STATUS_NOERROR;
}

J2534_API long __stdcall PassThruIoctl(unsigned long ChannelID,
                                        unsigned long IoctlID,
                                        void* pInput,
                                        void* pOutput) {
    unsigned char body[256];
    unsigned char resp[256];
    unsigned long resp_len;
    unsigned long body_len;
    long ret;

    // Build request
    write_uint32_be(body, ChannelID);
    write_uint32_be(body + 4, IoctlID);
    write_uint32_be(body + 8, 0);  // no input data for now
    body_len = 12;

    ret = send_recv(MSG_IOCTL_REQ, body, body_len, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        return ret;
    }

    if (resp_len >= 8) {
        unsigned long return_code = read_uint32_be(resp);
        unsigned long output_len = read_uint32_be(resp + 4);

        // Handle READ_VBATT - write voltage to pOutput
        if (IoctlID == READ_VBATT && pOutput != NULL && output_len >= 4) {
            unsigned long voltage = read_uint32_be(resp + 8);
            *((unsigned long*)pOutput) = voltage;
        }

        return return_code;
    }

    return ERR_FAILED;
}
