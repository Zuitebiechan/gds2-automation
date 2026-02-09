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
#include <time.h>

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
#define MSG_GET_LAST_ERROR_REQ  0x0021
#define MSG_GET_LAST_ERROR_RSP  0x8021
#define MSG_HEARTBEAT           0x00FF
#define MSG_HEARTBEAT_ACK       0x80FF

// Server configuration
static const char* SERVER_HOST = "127.0.0.1";
static const int SERVER_PORT = 9001;

// Socket recv timeout (ms) - prevents indefinite blocking
#define SOCKET_RECV_TIMEOUT_MS  30000

// Global state
static SOCKET g_socket = INVALID_SOCKET;
static BOOL g_initialized = FALSE;
static unsigned long g_sequence = 0;
static char g_last_error[256] = {0};
static CRITICAL_SECTION g_cs;

// Logging
static FILE* g_log_file = NULL;
static LARGE_INTEGER g_perf_freq;

// ============================================================================
// Logging
// ============================================================================

static void log_init(void) {
    char log_path[MAX_PATH];
    char* userprofile = getenv("USERPROFILE");
    if (userprofile) {
        sprintf_s(log_path, sizeof(log_path), "%s\\gds2-data\\vci_proxy_dll.log", userprofile);
    } else {
        strcpy_s(log_path, sizeof(log_path), "C:\\vci_proxy_dll.log");
    }

    g_log_file = fopen(log_path, "a");
    QueryPerformanceFrequency(&g_perf_freq);

    if (g_log_file) {
        fprintf(g_log_file, "\n========== DLL Loaded ==========\n");
        fflush(g_log_file);
    }
}

static void log_close(void) {
    if (g_log_file) {
        fprintf(g_log_file, "========== DLL Unloaded ==========\n");
        fclose(g_log_file);
        g_log_file = NULL;
    }
}

static double get_time_ms(void) {
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    return (double)now.QuadPart / (double)g_perf_freq.QuadPart * 1000.0;
}

static void log_msg(const char* fmt, ...) {
    if (!g_log_file) return;

    SYSTEMTIME st;
    GetLocalTime(&st);

    fprintf(g_log_file, "%02d:%02d:%02d.%03d | ",
            st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);

    va_list args;
    va_start(args, fmt);
    vfprintf(g_log_file, fmt, args);
    va_end(args);

    fprintf(g_log_file, "\n");
    fflush(g_log_file);
}

static const char* ioctl_name(unsigned long id) {
    switch (id) {
        case GET_CONFIG: return "GET_CONFIG";
        case SET_CONFIG: return "SET_CONFIG";
        case READ_VBATT: return "READ_VBATT";
        case FIVE_BAUD_INIT: return "FIVE_BAUD_INIT";
        case FAST_INIT: return "FAST_INIT";
        case CLEAR_TX_BUFFER: return "CLEAR_TX_BUFFER";
        case CLEAR_RX_BUFFER: return "CLEAR_RX_BUFFER";
        case CLEAR_PERIODIC_MSGS: return "CLEAR_PERIODIC_MSGS";
        case CLEAR_MSG_FILTERS: return "CLEAR_MSG_FILTERS";
        default: return "UNKNOWN";
    }
}

static const char* error_name(long code) {
    switch (code) {
        case STATUS_NOERROR: return "OK";
        case ERR_NOT_SUPPORTED: return "NOT_SUPPORTED";
        case ERR_INVALID_CHANNEL_ID: return "INVALID_CHANNEL_ID";
        case ERR_NULL_PARAMETER: return "NULL_PARAMETER";
        case ERR_FAILED: return "FAILED";
        case ERR_DEVICE_NOT_CONNECTED: return "DEVICE_NOT_CONNECTED";
        case ERR_TIMEOUT: return "TIMEOUT";
        case ERR_BUFFER_EMPTY: return "BUFFER_EMPTY";
        case ERR_BUFFER_OVERFLOW: return "BUFFER_OVERFLOW";
        default: return "OTHER";
    }
}

// ============================================================================
// Network helpers
// ============================================================================

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

// Receive exactly n bytes (handles partial recv)
static int recv_exact(SOCKET sock, char* buf, int len) {
    int total = 0;
    while (total < len) {
        int received = recv(sock, buf + total, len - total, 0);
        if (received <= 0) {
            return received;
        }
        total += received;
    }
    return total;
}

// Initialize Winsock and connect to server
static BOOL connect_to_server(void) {
    WSADATA wsaData;
    struct sockaddr_in serverAddr;
    DWORD timeout;

    if (g_socket != INVALID_SOCKET) {
        return TRUE;  // Already connected
    }

    log_msg("Connecting to %s:%d...", SERVER_HOST, SERVER_PORT);

    // Initialize Winsock
    if (WSAStartup(MAKEWORD(2, 2), &wsaData) != 0) {
        sprintf_s(g_last_error, sizeof(g_last_error), "WSAStartup failed");
        log_msg("ERROR: WSAStartup failed");
        return FALSE;
    }

    // Create socket
    g_socket = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (g_socket == INVALID_SOCKET) {
        sprintf_s(g_last_error, sizeof(g_last_error), "Socket creation failed");
        log_msg("ERROR: Socket creation failed");
        WSACleanup();
        return FALSE;
    }

    // Set recv timeout to avoid indefinite blocking
    timeout = SOCKET_RECV_TIMEOUT_MS;
    setsockopt(g_socket, SOL_SOCKET, SO_RCVTIMEO, (const char*)&timeout, sizeof(timeout));

    // Disable Nagle's algorithm for low latency
    {
        int flag = 1;
        setsockopt(g_socket, IPPROTO_TCP, TCP_NODELAY, (const char*)&flag, sizeof(flag));
    }

    // Connect to server
    memset(&serverAddr, 0, sizeof(serverAddr));
    serverAddr.sin_family = AF_INET;
    serverAddr.sin_port = htons(SERVER_PORT);
    inet_pton(AF_INET, SERVER_HOST, &serverAddr.sin_addr);

    if (connect(g_socket, (struct sockaddr*)&serverAddr, sizeof(serverAddr)) == SOCKET_ERROR) {
        sprintf_s(g_last_error, sizeof(g_last_error),
                  "Connection to %s:%d failed", SERVER_HOST, SERVER_PORT);
        log_msg("ERROR: Connection failed (WSA=%d)", WSAGetLastError());
        closesocket(g_socket);
        g_socket = INVALID_SOCKET;
        WSACleanup();
        return FALSE;
    }

    log_msg("Connected to server");
    return TRUE;
}

// Close socket and allow reconnection
static void disconnect_socket(void) {
    if (g_socket != INVALID_SOCKET) {
        closesocket(g_socket);
        g_socket = INVALID_SOCKET;
        log_msg("Socket disconnected");
    }
}

// Send message and receive response
static long send_recv(unsigned short msg_type, const unsigned char* body,
                      unsigned long body_len, unsigned char* resp_body,
                      unsigned long* resp_len, unsigned long max_resp_len) {
    unsigned char header[HEADER_SIZE];
    unsigned char resp_header[HEADER_SIZE];
    unsigned long total_len = HEADER_SIZE + body_len;
    unsigned long magic, length;
    unsigned short resp_type;
    unsigned long sequence;
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
        log_msg("ERROR: Send header failed (sent=%d, WSA=%d)", sent, WSAGetLastError());
        disconnect_socket();
        LeaveCriticalSection(&g_cs);
        return ERR_FAILED;
    }

    // Send body
    if (body_len > 0) {
        sent = send(g_socket, (const char*)body, body_len, 0);
        if (sent != (int)body_len) {
            log_msg("ERROR: Send body failed (sent=%d/%lu, WSA=%d)", sent, body_len, WSAGetLastError());
            disconnect_socket();
            LeaveCriticalSection(&g_cs);
            return ERR_FAILED;
        }
    }

    // Receive response header (use recv_exact for reliability)
    received = recv_exact(g_socket, (char*)resp_header, HEADER_SIZE);
    if (received != HEADER_SIZE) {
        log_msg("ERROR: Recv header failed (received=%d, WSA=%d)", received, WSAGetLastError());
        disconnect_socket();
        LeaveCriticalSection(&g_cs);
        return ERR_FAILED;
    }

    // Parse response header
    magic = read_uint32_be(resp_header);
    length = read_uint32_be(resp_header + 4);
    resp_type = read_uint16_be(resp_header + 8);
    sequence = read_uint32_be(resp_header + 10);

    if (magic != MAGIC) {
        log_msg("ERROR: Invalid magic: %08lx", magic);
        disconnect_socket();
        LeaveCriticalSection(&g_cs);
        return ERR_FAILED;
    }

    // Receive response body
    *resp_len = length - HEADER_SIZE;
    if (*resp_len > 0) {
        if (*resp_len > max_resp_len) {
            log_msg("ERROR: Response too large: %lu > %lu", *resp_len, max_resp_len);
            disconnect_socket();
            LeaveCriticalSection(&g_cs);
            return ERR_BUFFER_OVERFLOW;
        }
        received = recv_exact(g_socket, (char*)resp_body, *resp_len);
        if (received != (int)*resp_len) {
            log_msg("ERROR: Recv body failed (received=%d/%lu, WSA=%d)", received, *resp_len, WSAGetLastError());
            disconnect_socket();
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
            log_init();
            break;
        case DLL_PROCESS_DETACH:
            if (g_socket != INVALID_SOCKET) {
                closesocket(g_socket);
                WSACleanup();
            }
            log_close();
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
    double start_ms;

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

    start_ms = get_time_ms();
    log_msg(">> PassThruOpen(name=%s)", pName ? (const char*)pName : "NULL");

    ret = send_recv(MSG_OPEN_REQ, body, body_len, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        log_msg("<< PassThruOpen -> %s (%.1fms)", error_name(ret), get_time_ms() - start_ms);
        return ret;
    }

    if (resp_len >= 8) {
        unsigned long return_code = read_uint32_be(resp);
        *pDeviceID = read_uint32_be(resp + 4);
        log_msg("<< PassThruOpen -> %s, deviceId=%lu (%.1fms)",
                error_name(return_code), *pDeviceID, get_time_ms() - start_ms);
        return return_code;
    }

    log_msg("<< PassThruOpen -> FAILED (short response, %.1fms)", get_time_ms() - start_ms);
    return ERR_FAILED;
}

J2534_API long __stdcall PassThruClose(unsigned long DeviceID) {
    unsigned char body[4];
    unsigned char resp[16];
    unsigned long resp_len;
    long ret;
    double start_ms = get_time_ms();

    log_msg(">> PassThruClose(deviceId=%lu)", DeviceID);

    write_uint32_be(body, DeviceID);
    ret = send_recv(MSG_CLOSE_REQ, body, 4, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        log_msg("<< PassThruClose -> %s (%.1fms)", error_name(ret), get_time_ms() - start_ms);
        return ret;
    }

    if (resp_len >= 4) {
        long rc = read_uint32_be(resp);
        log_msg("<< PassThruClose -> %s (%.1fms)", error_name(rc), get_time_ms() - start_ms);
        return rc;
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
    double start_ms = get_time_ms();

    if (pChannelID == NULL) {
        return ERR_NULL_PARAMETER;
    }

    log_msg(">> PassThruConnect(dev=%lu, proto=%lu, flags=0x%lx, baud=%lu)",
            DeviceID, ProtocolID, Flags, BaudRate);

    write_uint32_be(body, DeviceID);
    write_uint32_be(body + 4, ProtocolID);
    write_uint32_be(body + 8, Flags);
    write_uint32_be(body + 12, BaudRate);

    ret = send_recv(MSG_CONNECT_REQ, body, 16, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        log_msg("<< PassThruConnect -> %s (%.1fms)", error_name(ret), get_time_ms() - start_ms);
        return ret;
    }

    if (resp_len >= 8) {
        unsigned long return_code = read_uint32_be(resp);
        *pChannelID = read_uint32_be(resp + 4);
        log_msg("<< PassThruConnect -> %s, channelId=%lu (%.1fms)",
                error_name(return_code), *pChannelID, get_time_ms() - start_ms);
        return return_code;
    }

    return ERR_FAILED;
}

J2534_API long __stdcall PassThruDisconnect(unsigned long ChannelID) {
    unsigned char body[4];
    unsigned char resp[16];
    unsigned long resp_len;
    long ret;
    double start_ms = get_time_ms();

    log_msg(">> PassThruDisconnect(ch=%lu)", ChannelID);

    write_uint32_be(body, ChannelID);
    ret = send_recv(MSG_DISCONNECT_REQ, body, 4, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        log_msg("<< PassThruDisconnect -> %s (%.1fms)", error_name(ret), get_time_ms() - start_ms);
        return ret;
    }

    if (resp_len >= 4) {
        long rc = read_uint32_be(resp);
        log_msg("<< PassThruDisconnect -> %s (%.1fms)", error_name(rc), get_time_ms() - start_ms);
        return rc;
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
    double start_ms = get_time_ms();

    if (pMsg == NULL || pNumMsgs == NULL) {
        return ERR_NULL_PARAMETER;
    }

    write_uint32_be(body, ChannelID);
    write_uint32_be(body + 4, *pNumMsgs);
    write_uint32_be(body + 8, Timeout);

    ret = send_recv(MSG_READ_MSGS_REQ, body, 12, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        log_msg("<< ReadMsgs(ch=%lu,t=%lu) -> %s (%.1fms)",
                ChannelID, Timeout, error_name(ret), get_time_ms() - start_ms);
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

    // Only log non-trivial results (avoid flooding on BUFFER_EMPTY)
    if (return_code != ERR_BUFFER_EMPTY && return_code != ERR_TIMEOUT) {
        log_msg("<< ReadMsgs(ch=%lu) -> %s, msgs=%lu (%.1fms)",
                ChannelID, error_name(return_code), i, get_time_ms() - start_ms);
    }

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
    double start_ms = get_time_ms();

    if (pMsg == NULL || pNumMsgs == NULL) {
        return ERR_NULL_PARAMETER;
    }

    log_msg(">> WriteMsgs(ch=%lu, n=%lu, t=%lu)", ChannelID, *pNumMsgs, Timeout);

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

    // Update actual message count in case loop broke early
    write_uint32_be(body + 4, i);

    ret = send_recv(MSG_WRITE_MSGS_REQ, body, body_len, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        log_msg("<< WriteMsgs -> %s (%.1fms)", error_name(ret), get_time_ms() - start_ms);
        return ret;
    }

    if (resp_len >= 8) {
        unsigned long return_code = read_uint32_be(resp);
        *pNumMsgs = read_uint32_be(resp + 4);
        log_msg("<< WriteMsgs -> %s, written=%lu (%.1fms)",
                error_name(return_code), *pNumMsgs, get_time_ms() - start_ms);
        return return_code;
    }

    return ERR_FAILED;
}

J2534_API long __stdcall PassThruStartPeriodicMsg(unsigned long ChannelID,
                                                   PASSTHRU_MSG* pMsg,
                                                   unsigned long* pMsgID,
                                                   unsigned long TimeInterval) {
    log_msg(">> StartPeriodicMsg(ch=%lu) -> NOT_SUPPORTED", ChannelID);
    return ERR_NOT_SUPPORTED;
}

J2534_API long __stdcall PassThruStopPeriodicMsg(unsigned long ChannelID,
                                                  unsigned long MsgID) {
    log_msg(">> StopPeriodicMsg(ch=%lu, msg=%lu) -> NOT_SUPPORTED", ChannelID, MsgID);
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
    double start_ms = get_time_ms();

    if (pFilterID == NULL) {
        return ERR_NULL_PARAMETER;
    }

    log_msg(">> StartMsgFilter(ch=%lu, type=%lu)", ChannelID, FilterType);

    // Header: ChannelID + FilterType
    write_uint32_be(body, ChannelID);
    write_uint32_be(body + 4, FilterType);
    body_len = 8;

    // Encode MaskMsg (present flag + message)
    if (pMaskMsg != NULL) {
        if (body_len + 1 + 20 + pMaskMsg->DataSize > sizeof(body)) {
            return ERR_FAILED;
        }
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
        if (body_len + 1 + 20 + pPatternMsg->DataSize > sizeof(body)) {
            return ERR_FAILED;
        }
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
        if (body_len + 1 + 20 + pFlowControlMsg->DataSize > sizeof(body)) {
            return ERR_FAILED;
        }
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
        log_msg("<< StartMsgFilter -> %s (%.1fms)", error_name(ret), get_time_ms() - start_ms);
        return ret;
    }

    if (resp_len >= 8) {
        unsigned long return_code = read_uint32_be(resp);
        *pFilterID = read_uint32_be(resp + 4);
        log_msg("<< StartMsgFilter -> %s, filterId=%lu (%.1fms)",
                error_name(return_code), *pFilterID, get_time_ms() - start_ms);
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
    double start_ms = get_time_ms();

    log_msg(">> StopMsgFilter(ch=%lu, filter=%lu)", ChannelID, FilterID);

    write_uint32_be(body, ChannelID);
    write_uint32_be(body + 4, FilterID);

    ret = send_recv(MSG_STOP_FILTER_REQ, body, 8, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        log_msg("<< StopMsgFilter -> %s (%.1fms)", error_name(ret), get_time_ms() - start_ms);
        return ret;
    }

    if (resp_len >= 4) {
        long rc = read_uint32_be(resp);
        log_msg("<< StopMsgFilter -> %s (%.1fms)", error_name(rc), get_time_ms() - start_ms);
        return rc;
    }

    return ERR_FAILED;
}

J2534_API long __stdcall PassThruSetProgrammingVoltage(unsigned long DeviceID,
                                                        unsigned long PinNumber,
                                                        unsigned long Voltage) {
    log_msg(">> SetProgrammingVoltage(dev=%lu) -> NOT_SUPPORTED", DeviceID);
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
    double start_ms = get_time_ms();

    if (pFirmwareVersion == NULL || pDllVersion == NULL || pApiVersion == NULL) {
        return ERR_NULL_PARAMETER;
    }

    log_msg(">> PassThruReadVersion(dev=%lu)", DeviceID);

    write_uint32_be(body, DeviceID);

    ret = send_recv(MSG_READ_VERSION_REQ, body, 4, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        log_msg("<< ReadVersion -> %s (%.1fms)", error_name(ret), get_time_ms() - start_ms);
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
        log_msg("<< ReadVersion -> %s (%.1fms)", error_name(return_code), get_time_ms() - start_ms);
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
    unsigned char body[4096];
    unsigned char resp[4096];
    unsigned long resp_len;
    unsigned long body_len;
    long ret;
    double start_ms = get_time_ms();

    log_msg(">> PassThruIoctl(ch=%lu, ioctl=%s[0x%02lx], in=%s, out=%s)",
            ChannelID, ioctl_name(IoctlID), IoctlID,
            pInput ? "yes" : "NULL", pOutput ? "yes" : "NULL");

    // Build request header
    write_uint32_be(body, ChannelID);
    write_uint32_be(body + 4, IoctlID);

    // Serialize input data based on IOCTL type
    if ((IoctlID == SET_CONFIG || IoctlID == GET_CONFIG) && pInput != NULL) {
        // SCONFIG_LIST: NumOfParams + array of {Parameter, Value}
        SCONFIG_LIST* pList = (SCONFIG_LIST*)pInput;
        unsigned long i;
        unsigned long input_len;

        // Bounds check to prevent buffer overflow
        if (pList->NumOfParams > 500) {
            log_msg("ERROR: NumOfParams too large: %lu", pList->NumOfParams);
            return ERR_FAILED;
        }

        input_len = 4 + pList->NumOfParams * 8;  // count + N*(param+value)
        if (12 + input_len > sizeof(body)) {
            log_msg("ERROR: SCONFIG data exceeds buffer: %lu", 12 + input_len);
            return ERR_FAILED;
        }

        write_uint32_be(body + 8, input_len);
        body_len = 12;

        // Write NumOfParams
        write_uint32_be(body + body_len, pList->NumOfParams);
        body_len += 4;

        // Write each SCONFIG entry
        for (i = 0; i < pList->NumOfParams; i++) {
            write_uint32_be(body + body_len, pList->ConfigPtr[i].Parameter);
            write_uint32_be(body + body_len + 4, pList->ConfigPtr[i].Value);
            log_msg("   param[%lu]: id=0x%04lx value=%lu",
                    i, pList->ConfigPtr[i].Parameter, pList->ConfigPtr[i].Value);
            body_len += 8;
        }
    } else {
        // No input data
        write_uint32_be(body + 8, 0);
        body_len = 12;
    }

    ret = send_recv(MSG_IOCTL_REQ, body, body_len, resp, &resp_len, sizeof(resp));
    if (ret != STATUS_NOERROR) {
        log_msg("<< Ioctl(%s) -> %s (%.1fms)", ioctl_name(IoctlID),
                error_name(ret), get_time_ms() - start_ms);
        return ret;
    }

    if (resp_len >= 8) {
        unsigned long return_code = read_uint32_be(resp);
        unsigned long output_len = read_uint32_be(resp + 4);

        // Handle READ_VBATT - write voltage to pOutput
        if (IoctlID == READ_VBATT && pOutput != NULL && output_len >= 4) {
            unsigned long voltage = read_uint32_be(resp + 8);
            *((unsigned long*)pOutput) = voltage;
            log_msg("<< Ioctl(READ_VBATT) -> %s, voltage=%lumV (%.1fms)",
                    error_name(return_code), voltage, get_time_ms() - start_ms);
        }
        // Handle GET_CONFIG - write values back to caller's SCONFIG_LIST (via pInput per J2534 spec)
        else if (IoctlID == GET_CONFIG && pInput != NULL && output_len >= 4) {
            SCONFIG_LIST* pList = (SCONFIG_LIST*)pInput;
            unsigned long num_params = read_uint32_be(resp + 8);
            unsigned long i;
            unsigned long off = 12;

            for (i = 0; i < num_params && i < pList->NumOfParams; i++) {
                if (off + 8 > resp_len) break;
                pList->ConfigPtr[i].Parameter = read_uint32_be(resp + off);
                pList->ConfigPtr[i].Value = read_uint32_be(resp + off + 4);
                log_msg("   got param[%lu]: id=0x%04lx value=%lu",
                        i, pList->ConfigPtr[i].Parameter, pList->ConfigPtr[i].Value);
                off += 8;
            }
            log_msg("<< Ioctl(GET_CONFIG) -> %s, %lu params (%.1fms)",
                    error_name(return_code), num_params, get_time_ms() - start_ms);
        }
        else {
            log_msg("<< Ioctl(%s) -> %s (%.1fms)", ioctl_name(IoctlID),
                    error_name(return_code), get_time_ms() - start_ms);
        }

        return return_code;
    }

    log_msg("<< Ioctl(%s) -> FAILED (short response, %.1fms)",
            ioctl_name(IoctlID), get_time_ms() - start_ms);
    return ERR_FAILED;
}
