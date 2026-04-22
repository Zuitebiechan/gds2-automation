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
#include <stdarg.h>

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
static int SERVER_PORT = 9001;  // overridden by VCI_PROXY_PORT env var

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
static FILE* g_jsonl_file = NULL;
static LARGE_INTEGER g_perf_freq;
static char g_component_instance_id[128] = {0};
static unsigned long g_readmsgs_buffer_empty_count = 0;
static double g_readmsgs_buffer_empty_last_emit_ms = 0.0;

static double get_time_ms(void);
static const char* error_name(long code);

static void ensure_dir_tree(const char* dir_path) {
    char temp[MAX_PATH];
    char* p;
    if (dir_path == NULL || !dir_path[0]) {
        return;
    }
    strcpy_s(temp, sizeof(temp), dir_path);
    for (p = temp + 3; *p; ++p) {
        if (*p == '\\' || *p == '/') {
            char saved = *p;
            *p = '\0';
            CreateDirectoryA(temp, NULL);
            *p = saved;
        }
    }
    CreateDirectoryA(temp, NULL);
}

static void json_escape(FILE* out, const char* value) {
    const unsigned char* p = (const unsigned char*)(value ? value : "");
    fputc('"', out);
    while (*p) {
        switch (*p) {
            case '\\': fputs("\\\\", out); break;
            case '"': fputs("\\\"", out); break;
            case '\n': fputs("\\n", out); break;
            case '\r': fputs("\\r", out); break;
            case '\t': fputs("\\t", out); break;
            default:
                if (*p < 0x20) {
                    fprintf(out, "\\u%04x", (unsigned int)*p);
                } else {
                    fputc(*p, out);
                }
                break;
        }
        ++p;
    }
    fputc('"', out);
}

static void jsonl_write_timestamp(FILE* out) {
    SYSTEMTIME st;
    GetSystemTime(&st);
    fprintf(
        out,
        "\"%04d-%02d-%02dT%02d:%02d:%02d.%03dZ\"",
        st.wYear,
        st.wMonth,
        st.wDay,
        st.wHour,
        st.wMinute,
        st.wSecond,
        st.wMilliseconds
    );
}

static const char* msg_name(unsigned short msg_type) {
    switch (msg_type) {
        case MSG_OPEN_REQ: return "OPEN_REQ";
        case MSG_CLOSE_REQ: return "CLOSE_REQ";
        case MSG_CONNECT_REQ: return "CONNECT_REQ";
        case MSG_DISCONNECT_REQ: return "DISCONNECT_REQ";
        case MSG_READ_MSGS_REQ: return "READ_MSGS_REQ";
        case MSG_WRITE_MSGS_REQ: return "WRITE_MSGS_REQ";
        case MSG_IOCTL_REQ: return "IOCTL_REQ";
        case MSG_START_FILTER_REQ: return "START_FILTER_REQ";
        case MSG_STOP_FILTER_REQ: return "STOP_FILTER_REQ";
        case MSG_READ_VERSION_REQ: return "READ_VERSION_REQ";
        default: return "UNKNOWN";
    }
}

static void jsonl_init(void) {
    char json_path[MAX_PATH];
    char dir_path[MAX_PATH];
    char* programdata = getenv("PROGRAMDATA");
    char* userprofile = getenv("USERPROFILE");
    DWORD pid = GetCurrentProcessId();
    SYSTEMTIME st;

    if (programdata && programdata[0]) {
        sprintf_s(
            json_path,
            sizeof(json_path),
            "%s\\RPA_Diagnostic\\observability\\cloud\\raw\\virtual_j2534-%lu.jsonl",
            programdata,
            (unsigned long)pid
        );
    } else if (userprofile && userprofile[0]) {
        sprintf_s(
            json_path,
            sizeof(json_path),
            "%s\\gds2-data\\virtual_j2534-observability.jsonl",
            userprofile
        );
    } else {
        strcpy_s(json_path, sizeof(json_path), "C:\\virtual_j2534-observability.jsonl");
    }

    strcpy_s(dir_path, sizeof(dir_path), json_path);
    {
        char* last_slash = strrchr(dir_path, '\\');
        if (last_slash != NULL) {
            *last_slash = '\0';
            ensure_dir_tree(dir_path);
        }
    }

    g_jsonl_file = fopen(json_path, "a");
    GetSystemTime(&st);
    sprintf_s(
        g_component_instance_id,
        sizeof(g_component_instance_id),
        "virtual_j2534:%lu:%04d%02d%02dT%02d%02d%02dZ",
        (unsigned long)pid,
        st.wYear,
        st.wMonth,
        st.wDay,
        st.wHour,
        st.wMinute,
        st.wSecond
    );
}

static void jsonl_close(void) {
    if (g_jsonl_file) {
        fclose(g_jsonl_file);
        g_jsonl_file = NULL;
    }
}

static void jsonl_emit_event(
    const char* event_type,
    const char* operation_kind,
    unsigned long dll_seq,
    const char* status,
    const char* failure_code,
    const char* failure_domain,
    const char* reason,
    double duration_ms,
    long return_code,
    unsigned short msg_type,
    unsigned long payload_len,
    unsigned long extra_value,
    const char* extra_key
) {
    if (!g_jsonl_file) return;

    fputc('{', g_jsonl_file);
    fputs("\"schema_version\":\"observability.v1\",", g_jsonl_file);
    fputs("\"ts\":", g_jsonl_file);
    jsonl_write_timestamp(g_jsonl_file);
    fputs(",\"component\":\"virtual_j2534\",", g_jsonl_file);
    fputs("\"component_instance_id\":", g_jsonl_file);
    json_escape(g_jsonl_file, g_component_instance_id);
    fputs(",\"event_type\":", g_jsonl_file);
    json_escape(g_jsonl_file, event_type);
    fputs(",\"session_id\":null,", g_jsonl_file);
    fputs("\"connection_epoch\":null,", g_jsonl_file);
    fprintf(g_jsonl_file, "\"dll_seq\":%lu,", dll_seq);
    fputs("\"proxy_seq\":null,", g_jsonl_file);
    fputs("\"worker_request_id\":null,", g_jsonl_file);
    fputs("\"operation_kind\":", g_jsonl_file);
    json_escape(g_jsonl_file, operation_kind ? operation_kind : "");
    fputs(",\"status\":", g_jsonl_file);
    json_escape(g_jsonl_file, status ? status : "ok");
    fputs(",\"failure_code\":", g_jsonl_file);
    if (failure_code) json_escape(g_jsonl_file, failure_code); else fputs("null", g_jsonl_file);
    fputs(",\"failure_domain\":", g_jsonl_file);
    json_escape(g_jsonl_file, failure_domain ? failure_domain : "unknown");
    fputs(",\"reason\":", g_jsonl_file);
    if (reason) json_escape(g_jsonl_file, reason); else fputs("null", g_jsonl_file);
    fprintf(g_jsonl_file, ",\"duration_ms\":%.3f", duration_ms >= 0.0 ? duration_ms : 0.0);
    fputs(",\"hw_ms\":null,", g_jsonl_file);
    fputs("\"network_ms\":null,", g_jsonl_file);
    fputs("\"page\":null,\"module\":null,\"data_category\":null,", g_jsonl_file);
    fputs("\"symptom\":null,\"impact_scope\":\"virtual_j2534\",", g_jsonl_file);
    fputs("\"next_checks\":[],\"redaction_applied\":[],", g_jsonl_file);
    fputs("\"j2534_method\":", g_jsonl_file);
    json_escape(g_jsonl_file, operation_kind ? operation_kind : "");
    fputs(",\"msg_name\":", g_jsonl_file);
    json_escape(g_jsonl_file, msg_name(msg_type));
    fprintf(g_jsonl_file, ",\"payload_length\":%lu", payload_len);
    if (return_code >= 0) {
        fprintf(g_jsonl_file, ",\"return_code\":%ld", return_code);
    } else {
        fputs(",\"return_code\":null", g_jsonl_file);
    }
    if (extra_key != NULL && extra_key[0]) {
        fputs(",\"", g_jsonl_file);
        fputs(extra_key, g_jsonl_file);
        fprintf(g_jsonl_file, "\":%lu", extra_value);
    }
    fputs("}\n", g_jsonl_file);
    fflush(g_jsonl_file);
}

static void jsonl_flush_readmsgs_buffer_empty(void) {
    double now_ms = get_time_ms();
    if (g_readmsgs_buffer_empty_count == 0) {
        return;
    }
    if ((now_ms - g_readmsgs_buffer_empty_last_emit_ms) < 5000.0 &&
        g_readmsgs_buffer_empty_count < 50) {
        return;
    }
    jsonl_emit_event(
        "j2534.read_msgs.buffer_empty_aggregate",
        "PassThruReadMsgs",
        0,
        "ok",
        NULL,
        "unknown",
        "BUFFER_EMPTY",
        0.0,
        ERR_BUFFER_EMPTY,
        MSG_READ_MSGS_REQ,
        0,
        g_readmsgs_buffer_empty_count,
        "buffer_empty_count"
    );
    g_readmsgs_buffer_empty_count = 0;
    g_readmsgs_buffer_empty_last_emit_ms = now_ms;
}

static void emit_j2534_call_started(
    const char* method,
    unsigned long dll_seq,
    unsigned short msg_type,
    unsigned long payload_len
) {
    jsonl_emit_event(
        "j2534.call.started",
        method,
        dll_seq,
        "started",
        NULL,
        "unknown",
        "call_started",
        0.0,
        -1,
        msg_type,
        payload_len,
        0,
        NULL
    );
}

static void emit_j2534_call_finished(
    const char* method,
    unsigned long dll_seq,
    unsigned short msg_type,
    unsigned long payload_len,
    double duration_ms,
    long return_code
) {
    jsonl_emit_event(
        "j2534.call.finished",
        method,
        dll_seq,
        "ok",
        NULL,
        "unknown",
        error_name(return_code),
        duration_ms,
        return_code,
        msg_type,
        payload_len,
        0,
        NULL
    );
}

static void emit_j2534_call_failed(
    const char* method,
    unsigned long dll_seq,
    unsigned short msg_type,
    unsigned long payload_len,
    double duration_ms,
    long return_code,
    const char* reason
) {
    jsonl_emit_event(
        "j2534.call.failed",
        method,
        dll_seq,
        "error",
        error_name(return_code),
        "cloud_dll_local_proxy",
        reason,
        duration_ms,
        return_code,
        msg_type,
        payload_len,
        0,
        NULL
    );
}

// ============================================================================
// Logging
// ============================================================================

// Log rotation threshold (10 MB)
#define LOG_MAX_SIZE (10 * 1024 * 1024)

static void log_init(void) {
    char log_path[MAX_PATH];
    char old_path[MAX_PATH];
    char* userprofile = getenv("USERPROFILE");
    if (userprofile) {
        sprintf_s(log_path, sizeof(log_path), "%s\\gds2-data\\vci_proxy_dll.log", userprofile);
        sprintf_s(old_path, sizeof(old_path), "%s\\gds2-data\\vci_proxy_dll.log.old", userprofile);
    } else {
        strcpy_s(log_path, sizeof(log_path), "C:\\vci_proxy_dll.log");
        strcpy_s(old_path, sizeof(old_path), "C:\\vci_proxy_dll.log.old");
    }

    // Log rotation: if log file exceeds threshold, rotate
    {
        WIN32_FILE_ATTRIBUTE_DATA fileInfo;
        if (GetFileAttributesExA(log_path, GetFileExInfoStandard, &fileInfo)) {
            ULONGLONG fileSize = ((ULONGLONG)fileInfo.nFileSizeHigh << 32) | fileInfo.nFileSizeLow;
            if (fileSize > LOG_MAX_SIZE) {
                DeleteFileA(old_path);
                MoveFileA(log_path, old_path);
            }
        }
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

// Send exactly n bytes (handles partial send)
static int send_exact(SOCKET sock, const char* buf, int len) {
    int total = 0;
    while (total < len) {
        int sent = send(sock, buf + total, len - total, 0);
        if (sent <= 0) {
            return sent;
        }
        total += sent;
    }
    return total;
}

// Initialize Winsock and connect to server
static BOOL connect_to_server(void) {
    struct sockaddr_in serverAddr;
    DWORD timeout;

    if (g_socket != INVALID_SOCKET) {
        return TRUE;  // Already connected
    }

    log_msg("Connecting to %s:%d...", SERVER_HOST, SERVER_PORT);

    // Create socket
    g_socket = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (g_socket == INVALID_SOCKET) {
        sprintf_s(g_last_error, sizeof(g_last_error), "Socket creation failed");
        log_msg("ERROR: Socket creation failed");
        jsonl_emit_event(
            "dll.socket.connect_failed",
            "socket_connect",
            0,
            "error",
            "socket_creation_failed",
            "cloud_dll_local_proxy",
            "socket creation failed",
            0.0,
            -1,
            0,
            0,
            0,
            NULL
        );
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
        jsonl_emit_event(
            "dll.socket.connect_failed",
            "socket_connect",
            0,
            "error",
            "connect_failed",
            "cloud_dll_local_proxy",
            g_last_error,
            0.0,
            -1,
            0,
            0,
            (unsigned long)WSAGetLastError(),
            "wsa_error"
        );
        closesocket(g_socket);
        g_socket = INVALID_SOCKET;
        return FALSE;
    }

    log_msg("Connected to server");
    jsonl_emit_event(
        "dll.socket.connected",
        "socket_connect",
        0,
        "ok",
        NULL,
        "unknown",
        "connected",
        0.0,
        -1,
        0,
        0,
        0,
        NULL
    );
    return TRUE;
}

// Close socket and allow reconnection
static void disconnect_socket(void) {
    if (g_socket != INVALID_SOCKET) {
        closesocket(g_socket);
        g_socket = INVALID_SOCKET;
        log_msg("Socket disconnected");
        jsonl_emit_event(
            "dll.socket.disconnected",
            "socket_disconnect",
            0,
            "error",
            "socket_disconnected",
            "cloud_dll_local_proxy",
            "socket disconnected",
            0.0,
            -1,
            0,
            0,
            0,
            NULL
        );
    }
}

// Retry delays in milliseconds
static const DWORD RETRY_DELAYS[] = { 100, 500, 1000 };
#define MAX_RETRIES 3

// Send message and receive response (with retry on transient failures)
static long send_recv(unsigned short msg_type, const unsigned char* body,
                      unsigned long body_len, unsigned char* resp_body,
                      unsigned long* resp_len, unsigned long max_resp_len,
                      unsigned long* used_sequence) {
    unsigned char header[HEADER_SIZE];
    unsigned char resp_header[HEADER_SIZE];
    unsigned long total_len = HEADER_SIZE + body_len;
    unsigned long magic, length;
    unsigned short resp_type;
    unsigned long sequence;
    int sent, received;
    int retry;

    EnterCriticalSection(&g_cs);

    g_sequence++;
    sequence = g_sequence;
    if (used_sequence != NULL) {
        *used_sequence = sequence;
    }

    for (retry = 0; retry <= MAX_RETRIES; retry++) {
        if (retry > 0) {
            log_msg("RETRY %d/%d for seq=%lu (delay=%lums)",
                    retry, MAX_RETRIES, sequence, RETRY_DELAYS[retry - 1]);
            jsonl_emit_event(
                "dll.request.retry",
                msg_name(msg_type),
                sequence,
                "error",
                "retry",
                "cloud_dll_local_proxy",
                "transient transport failure",
                0.0,
                -1,
                msg_type,
                body_len,
                (unsigned long)retry,
                "retry_index"
            );
            Sleep(RETRY_DELAYS[retry - 1]);
        }

        if (!connect_to_server()) {
            continue;
        }

        // Build header
        write_uint32_be(header, MAGIC);
        write_uint32_be(header + 4, total_len);
        write_uint16_be(header + 8, msg_type);
        write_uint32_be(header + 10, sequence);

        // Send header
        sent = send_exact(g_socket, (const char*)header, HEADER_SIZE);
        if (sent != HEADER_SIZE) {
            log_msg("ERROR: Send header failed (sent=%d, WSA=%d)", sent, WSAGetLastError());
            jsonl_emit_event(
                "dll.socket.send_failed",
                msg_name(msg_type),
                sequence,
                "error",
                "send_header_failed",
                "cloud_dll_local_proxy",
                "send header failed",
                0.0,
                -1,
                msg_type,
                body_len,
                (unsigned long)WSAGetLastError(),
                "wsa_error"
            );
            disconnect_socket();
            continue;
        }

        // Send body
        if (body_len > 0) {
            sent = send_exact(g_socket, (const char*)body, body_len);
            if (sent != (int)body_len) {
                log_msg("ERROR: Send body failed (sent=%d/%lu, WSA=%d)", sent, body_len, WSAGetLastError());
                jsonl_emit_event(
                    "dll.socket.send_failed",
                    msg_name(msg_type),
                    sequence,
                    "error",
                    "send_body_failed",
                    "cloud_dll_local_proxy",
                    "send body failed",
                    0.0,
                    -1,
                    msg_type,
                    body_len,
                    (unsigned long)WSAGetLastError(),
                    "wsa_error"
                );
                disconnect_socket();
                continue;
            }
        }

        // Receive response header
        received = recv_exact(g_socket, (char*)resp_header, HEADER_SIZE);
        if (received != HEADER_SIZE) {
            log_msg("ERROR: Recv header failed (received=%d, WSA=%d)", received, WSAGetLastError());
            jsonl_emit_event(
                "dll.socket.recv_failed",
                msg_name(msg_type),
                sequence,
                "error",
                "recv_header_failed",
                "cloud_dll_local_proxy",
                "recv header failed",
                0.0,
                -1,
                msg_type,
                body_len,
                (unsigned long)WSAGetLastError(),
                "wsa_error"
            );
            disconnect_socket();
            continue;
        }

        // Parse response header
        magic = read_uint32_be(resp_header);
        length = read_uint32_be(resp_header + 4);
        resp_type = read_uint16_be(resp_header + 8);

        if (magic != MAGIC) {
            log_msg("ERROR: Invalid magic: %08lx", magic);
            disconnect_socket();
            continue;
        }

        // Receive response body
        if (length < HEADER_SIZE) {
            log_msg("ERROR: Response length too small: %lu < %u", length, HEADER_SIZE);
            disconnect_socket();
            continue;
        }
        *resp_len = length - HEADER_SIZE;
        if (*resp_len > 0) {
            if (*resp_len > max_resp_len) {
                log_msg("ERROR: Response too large: %lu > %lu", *resp_len, max_resp_len);
                disconnect_socket();
                LeaveCriticalSection(&g_cs);
                return ERR_BUFFER_OVERFLOW;  // Protocol error, not transient
            }
            received = recv_exact(g_socket, (char*)resp_body, *resp_len);
            if (received != (int)*resp_len) {
                log_msg("ERROR: Recv body failed (received=%d/%lu, WSA=%d)", received, *resp_len, WSAGetLastError());
                jsonl_emit_event(
                    "dll.socket.recv_failed",
                    msg_name(msg_type),
                    sequence,
                    "error",
                    "recv_body_failed",
                    "cloud_dll_local_proxy",
                    "recv body failed",
                    0.0,
                    -1,
                    msg_type,
                    body_len,
                    (unsigned long)WSAGetLastError(),
                    "wsa_error"
                );
                disconnect_socket();
                continue;
            }
        }

        // Success
        if (retry > 0) {
            log_msg("RETRY succeeded on attempt %d for seq=%lu", retry + 1, sequence);
            jsonl_emit_event(
                "dll.request.retry_succeeded",
                msg_name(msg_type),
                sequence,
                "ok",
                NULL,
                "unknown",
                "retry succeeded",
                0.0,
                -1,
                msg_type,
                body_len,
                (unsigned long)(retry + 1),
                "attempt"
            );
        }
        LeaveCriticalSection(&g_cs);
        return STATUS_NOERROR;
    }

    // All retries exhausted
    log_msg("ERROR: All %d retries exhausted for seq=%lu", MAX_RETRIES + 1, sequence);
    jsonl_emit_event(
        "dll.request.retry_exhausted",
        msg_name(msg_type),
        sequence,
        "error",
        "retry_exhausted",
        "cloud_dll_local_proxy",
        "all retries exhausted",
        0.0,
        ERR_DEVICE_NOT_CONNECTED,
        msg_type,
        body_len,
        (unsigned long)(MAX_RETRIES + 1),
        "attempts"
    );
    LeaveCriticalSection(&g_cs);
    return ERR_DEVICE_NOT_CONNECTED;
}

// DLL Entry Point
BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpvReserved) {
    switch (fdwReason) {
        case DLL_PROCESS_ATTACH:
            {
                WSADATA wsaData;
                char port_buf[16];
                InitializeCriticalSection(&g_cs);
                WSAStartup(MAKEWORD(2, 2), &wsaData);
                g_initialized = TRUE;
                log_init();
                jsonl_init();
                if (GetEnvironmentVariableA("VCI_PROXY_PORT", port_buf, sizeof(port_buf))) {
                    int port = atoi(port_buf);
                    if (port > 0 && port < 65536) {
                        SERVER_PORT = port;
                    }
                }
                log_msg("VCI Proxy DLL loaded, target port=%d", SERVER_PORT);
            }
            break;
        case DLL_PROCESS_DETACH:
            if (g_socket != INVALID_SOCKET) {
                closesocket(g_socket);
                g_socket = INVALID_SOCKET;
            }
            jsonl_flush_readmsgs_buffer_empty();
            WSACleanup();
            log_close();
            jsonl_close();
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
    unsigned long dll_seq = 0;
    long ret;
    double start_ms;

    if (pDeviceID == NULL) {
        return ERR_NULL_PARAMETER;
    }

    // Encode device name (optional)
    if (pName != NULL) {
        const char* name = (const char*)pName;
        size_t name_len = strlen(name);
        if (name_len > sizeof(body) - 1) {
            log_msg("ERROR: Device name too long: %zu > %zu", name_len, sizeof(body) - 1);
            return ERR_FAILED;
        }
        if (name_len > 0) {
            memcpy(body, name, name_len);
            body[name_len] = 0;
            body_len = (unsigned long)(name_len + 1);
        }
    }

    start_ms = get_time_ms();
    log_msg(">> PassThruOpen(name=%s)", pName ? (const char*)pName : "NULL");
    emit_j2534_call_started("PassThruOpen", g_sequence + 1, MSG_OPEN_REQ, body_len);

    ret = send_recv(MSG_OPEN_REQ, body, body_len, resp, &resp_len, sizeof(resp), &dll_seq);
    if (ret != STATUS_NOERROR) {
        log_msg("<< PassThruOpen -> %s (%.1fms)", error_name(ret), get_time_ms() - start_ms);
        emit_j2534_call_failed("PassThruOpen", dll_seq, MSG_OPEN_REQ, body_len, get_time_ms() - start_ms, ret, "transport_failed");
        return ret;
    }

    if (resp_len >= 8) {
        unsigned long return_code = read_uint32_be(resp);
        *pDeviceID = read_uint32_be(resp + 4);
        log_msg("<< PassThruOpen -> %s, deviceId=%lu (%.1fms)",
                error_name(return_code), *pDeviceID, get_time_ms() - start_ms);
        if (return_code == STATUS_NOERROR) {
            emit_j2534_call_finished("PassThruOpen", dll_seq, MSG_OPEN_REQ, body_len, get_time_ms() - start_ms, return_code);
        } else {
            emit_j2534_call_failed("PassThruOpen", dll_seq, MSG_OPEN_REQ, body_len, get_time_ms() - start_ms, return_code, "remote_return_code");
        }
        return return_code;
    }

    log_msg("<< PassThruOpen -> FAILED (short response, %.1fms)", get_time_ms() - start_ms);
    emit_j2534_call_failed("PassThruOpen", dll_seq, MSG_OPEN_REQ, body_len, get_time_ms() - start_ms, ERR_FAILED, "short_response");
    return ERR_FAILED;
}

J2534_API long __stdcall PassThruClose(unsigned long DeviceID) {
    unsigned char body[4];
    unsigned char resp[16];
    unsigned long resp_len;
    unsigned long dll_seq = 0;
    long ret;
    double start_ms = get_time_ms();

    log_msg(">> PassThruClose(deviceId=%lu)", DeviceID);
    emit_j2534_call_started("PassThruClose", g_sequence + 1, MSG_CLOSE_REQ, 4);

    write_uint32_be(body, DeviceID);
    ret = send_recv(MSG_CLOSE_REQ, body, 4, resp, &resp_len, sizeof(resp), &dll_seq);
    if (ret != STATUS_NOERROR) {
        log_msg("<< PassThruClose -> %s (%.1fms)", error_name(ret), get_time_ms() - start_ms);
        emit_j2534_call_failed("PassThruClose", dll_seq, MSG_CLOSE_REQ, 4, get_time_ms() - start_ms, ret, "transport_failed");
        return ret;
    }

    if (resp_len >= 4) {
        long rc = read_uint32_be(resp);
        log_msg("<< PassThruClose -> %s (%.1fms)", error_name(rc), get_time_ms() - start_ms);
        if (rc == STATUS_NOERROR) {
            emit_j2534_call_finished("PassThruClose", dll_seq, MSG_CLOSE_REQ, 4, get_time_ms() - start_ms, rc);
        } else {
            emit_j2534_call_failed("PassThruClose", dll_seq, MSG_CLOSE_REQ, 4, get_time_ms() - start_ms, rc, "remote_return_code");
        }
        return rc;
    }

    emit_j2534_call_failed("PassThruClose", dll_seq, MSG_CLOSE_REQ, 4, get_time_ms() - start_ms, ERR_FAILED, "short_response");
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
    unsigned long dll_seq = 0;
    long ret;
    double start_ms = get_time_ms();

    if (pChannelID == NULL) {
        return ERR_NULL_PARAMETER;
    }

    log_msg(">> PassThruConnect(dev=%lu, proto=%lu, flags=0x%lx, baud=%lu)",
            DeviceID, ProtocolID, Flags, BaudRate);
    emit_j2534_call_started("PassThruConnect", g_sequence + 1, MSG_CONNECT_REQ, 16);

    write_uint32_be(body, DeviceID);
    write_uint32_be(body + 4, ProtocolID);
    write_uint32_be(body + 8, Flags);
    write_uint32_be(body + 12, BaudRate);

    ret = send_recv(MSG_CONNECT_REQ, body, 16, resp, &resp_len, sizeof(resp), &dll_seq);
    if (ret != STATUS_NOERROR) {
        log_msg("<< PassThruConnect -> %s (%.1fms)", error_name(ret), get_time_ms() - start_ms);
        emit_j2534_call_failed("PassThruConnect", dll_seq, MSG_CONNECT_REQ, 16, get_time_ms() - start_ms, ret, "transport_failed");
        return ret;
    }

    if (resp_len >= 8) {
        unsigned long return_code = read_uint32_be(resp);
        *pChannelID = read_uint32_be(resp + 4);
        log_msg("<< PassThruConnect -> %s, channelId=%lu (%.1fms)",
                error_name(return_code), *pChannelID, get_time_ms() - start_ms);
        if (return_code == STATUS_NOERROR) {
            emit_j2534_call_finished("PassThruConnect", dll_seq, MSG_CONNECT_REQ, 16, get_time_ms() - start_ms, return_code);
        } else {
            emit_j2534_call_failed("PassThruConnect", dll_seq, MSG_CONNECT_REQ, 16, get_time_ms() - start_ms, return_code, "remote_return_code");
        }
        return return_code;
    }

    emit_j2534_call_failed("PassThruConnect", dll_seq, MSG_CONNECT_REQ, 16, get_time_ms() - start_ms, ERR_FAILED, "short_response");
    return ERR_FAILED;
}

J2534_API long __stdcall PassThruDisconnect(unsigned long ChannelID) {
    unsigned char body[4];
    unsigned char resp[16];
    unsigned long resp_len;
    unsigned long dll_seq = 0;
    long ret;
    double start_ms = get_time_ms();

    log_msg(">> PassThruDisconnect(ch=%lu)", ChannelID);
    emit_j2534_call_started("PassThruDisconnect", g_sequence + 1, MSG_DISCONNECT_REQ, 4);

    write_uint32_be(body, ChannelID);
    ret = send_recv(MSG_DISCONNECT_REQ, body, 4, resp, &resp_len, sizeof(resp), &dll_seq);
    if (ret != STATUS_NOERROR) {
        log_msg("<< PassThruDisconnect -> %s (%.1fms)", error_name(ret), get_time_ms() - start_ms);
        emit_j2534_call_failed("PassThruDisconnect", dll_seq, MSG_DISCONNECT_REQ, 4, get_time_ms() - start_ms, ret, "transport_failed");
        return ret;
    }

    if (resp_len >= 4) {
        long rc = read_uint32_be(resp);
        log_msg("<< PassThruDisconnect -> %s (%.1fms)", error_name(rc), get_time_ms() - start_ms);
        if (rc == STATUS_NOERROR) {
            emit_j2534_call_finished("PassThruDisconnect", dll_seq, MSG_DISCONNECT_REQ, 4, get_time_ms() - start_ms, rc);
        } else {
            emit_j2534_call_failed("PassThruDisconnect", dll_seq, MSG_DISCONNECT_REQ, 4, get_time_ms() - start_ms, rc, "remote_return_code");
        }
        return rc;
    }

    emit_j2534_call_failed("PassThruDisconnect", dll_seq, MSG_DISCONNECT_REQ, 4, get_time_ms() - start_ms, ERR_FAILED, "short_response");
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
    unsigned long dll_seq = 0;
    double start_ms = get_time_ms();

    if (pMsg == NULL || pNumMsgs == NULL) {
        return ERR_NULL_PARAMETER;
    }

    write_uint32_be(body, ChannelID);
    write_uint32_be(body + 4, *pNumMsgs);
    write_uint32_be(body + 8, Timeout);
    emit_j2534_call_started("PassThruReadMsgs", g_sequence + 1, MSG_READ_MSGS_REQ, 12);

    ret = send_recv(MSG_READ_MSGS_REQ, body, 12, resp, &resp_len, sizeof(resp), &dll_seq);
    if (ret != STATUS_NOERROR) {
        log_msg("<< ReadMsgs(ch=%lu,t=%lu) -> %s (%.1fms)",
                ChannelID, Timeout, error_name(ret), get_time_ms() - start_ms);
        emit_j2534_call_failed("PassThruReadMsgs", dll_seq, MSG_READ_MSGS_REQ, 12, get_time_ms() - start_ms, ret, "transport_failed");
        return ret;
    }

    if (resp_len < 8) {
        return ERR_FAILED;
    }

    return_code = read_uint32_be(resp);
    num_msgs = read_uint32_be(resp + 4);

    offset = 8;
    for (i = 0; i < num_msgs && i < *pNumMsgs; i++) {
        unsigned long proto, rxstat, txflags, tstamp, datasize;
        if (offset + 20 > resp_len) break;

        proto = read_uint32_be(resp + offset);
        rxstat = read_uint32_be(resp + offset + 4);
        txflags = read_uint32_be(resp + offset + 8);
        tstamp = read_uint32_be(resp + offset + 12);
        datasize = read_uint32_be(resp + offset + 16);
        offset += 20;

        if (datasize > sizeof(pMsg[i].Data)) datasize = sizeof(pMsg[i].Data);
        if (offset + datasize > resp_len) break;

        pMsg[i].ProtocolID = proto;
        pMsg[i].RxStatus = rxstat;
        pMsg[i].TxFlags = txflags;
        pMsg[i].Timestamp = tstamp;
        pMsg[i].DataSize = datasize;
        memcpy(pMsg[i].Data, resp + offset, datasize);
        offset += datasize;
    }

    *pNumMsgs = i;

    // Only log non-trivial results (avoid flooding on BUFFER_EMPTY)
    if (return_code != ERR_BUFFER_EMPTY && return_code != ERR_TIMEOUT) {
        log_msg("<< ReadMsgs(ch=%lu) -> %s, msgs=%lu (%.1fms)",
                ChannelID, error_name(return_code), i, get_time_ms() - start_ms);
        if (return_code == STATUS_NOERROR) {
            jsonl_flush_readmsgs_buffer_empty();
            emit_j2534_call_finished("PassThruReadMsgs", dll_seq, MSG_READ_MSGS_REQ, 12, get_time_ms() - start_ms, return_code);
        } else {
            emit_j2534_call_failed("PassThruReadMsgs", dll_seq, MSG_READ_MSGS_REQ, 12, get_time_ms() - start_ms, return_code, "remote_return_code");
        }
    } else if (return_code == ERR_BUFFER_EMPTY) {
        g_readmsgs_buffer_empty_count++;
        jsonl_flush_readmsgs_buffer_empty();
    } else {
        emit_j2534_call_failed("PassThruReadMsgs", dll_seq, MSG_READ_MSGS_REQ, 12, get_time_ms() - start_ms, return_code, "remote_return_code");
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
    unsigned long dll_seq = 0;
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
        if (body_len + 20 + pMsg[i].DataSize > sizeof(body)) break;
        write_uint32_be(body + body_len, pMsg[i].ProtocolID);
        write_uint32_be(body + body_len + 4, pMsg[i].RxStatus);
        write_uint32_be(body + body_len + 8, pMsg[i].TxFlags);
        write_uint32_be(body + body_len + 12, pMsg[i].Timestamp);
        write_uint32_be(body + body_len + 16, pMsg[i].DataSize);
        body_len += 20;

        memcpy(body + body_len, pMsg[i].Data, pMsg[i].DataSize);
        body_len += pMsg[i].DataSize;
    }

    // Update actual message count in case loop broke early
    write_uint32_be(body + 4, i);
    emit_j2534_call_started("PassThruWriteMsgs", g_sequence + 1, MSG_WRITE_MSGS_REQ, body_len);

    ret = send_recv(MSG_WRITE_MSGS_REQ, body, body_len, resp, &resp_len, sizeof(resp), &dll_seq);
    if (ret != STATUS_NOERROR) {
        log_msg("<< WriteMsgs -> %s (%.1fms)", error_name(ret), get_time_ms() - start_ms);
        emit_j2534_call_failed("PassThruWriteMsgs", dll_seq, MSG_WRITE_MSGS_REQ, body_len, get_time_ms() - start_ms, ret, "transport_failed");
        return ret;
    }

    if (resp_len >= 8) {
        unsigned long return_code = read_uint32_be(resp);
        *pNumMsgs = read_uint32_be(resp + 4);
        log_msg("<< WriteMsgs -> %s, written=%lu (%.1fms)",
                error_name(return_code), *pNumMsgs, get_time_ms() - start_ms);
        if (return_code == STATUS_NOERROR) {
            emit_j2534_call_finished("PassThruWriteMsgs", dll_seq, MSG_WRITE_MSGS_REQ, body_len, get_time_ms() - start_ms, return_code);
        } else {
            emit_j2534_call_failed("PassThruWriteMsgs", dll_seq, MSG_WRITE_MSGS_REQ, body_len, get_time_ms() - start_ms, return_code, "remote_return_code");
        }
        return return_code;
    }

    emit_j2534_call_failed("PassThruWriteMsgs", dll_seq, MSG_WRITE_MSGS_REQ, body_len, get_time_ms() - start_ms, ERR_FAILED, "short_response");
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
    unsigned long dll_seq = 0;
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

    emit_j2534_call_started("PassThruStartMsgFilter", g_sequence + 1, MSG_START_FILTER_REQ, body_len);
    ret = send_recv(MSG_START_FILTER_REQ, body, body_len, resp, &resp_len, sizeof(resp), &dll_seq);
    if (ret != STATUS_NOERROR) {
        log_msg("<< StartMsgFilter -> %s (%.1fms)", error_name(ret), get_time_ms() - start_ms);
        emit_j2534_call_failed("PassThruStartMsgFilter", dll_seq, MSG_START_FILTER_REQ, body_len, get_time_ms() - start_ms, ret, "transport_failed");
        return ret;
    }

    if (resp_len >= 8) {
        unsigned long return_code = read_uint32_be(resp);
        *pFilterID = read_uint32_be(resp + 4);
        log_msg("<< StartMsgFilter -> %s, filterId=%lu (%.1fms)",
                error_name(return_code), *pFilterID, get_time_ms() - start_ms);
        if (return_code == STATUS_NOERROR) {
            emit_j2534_call_finished("PassThruStartMsgFilter", dll_seq, MSG_START_FILTER_REQ, body_len, get_time_ms() - start_ms, return_code);
        } else {
            emit_j2534_call_failed("PassThruStartMsgFilter", dll_seq, MSG_START_FILTER_REQ, body_len, get_time_ms() - start_ms, return_code, "remote_return_code");
        }
        return return_code;
    }

    emit_j2534_call_failed("PassThruStartMsgFilter", dll_seq, MSG_START_FILTER_REQ, body_len, get_time_ms() - start_ms, ERR_FAILED, "short_response");
    return ERR_FAILED;
}

J2534_API long __stdcall PassThruStopMsgFilter(unsigned long ChannelID,
                                                unsigned long FilterID) {
    unsigned char body[8];
    unsigned char resp[16];
    unsigned long resp_len;
    unsigned long dll_seq = 0;
    long ret;
    double start_ms = get_time_ms();

    log_msg(">> StopMsgFilter(ch=%lu, filter=%lu)", ChannelID, FilterID);

    write_uint32_be(body, ChannelID);
    write_uint32_be(body + 4, FilterID);

    emit_j2534_call_started("PassThruStopMsgFilter", g_sequence + 1, MSG_STOP_FILTER_REQ, 8);
    ret = send_recv(MSG_STOP_FILTER_REQ, body, 8, resp, &resp_len, sizeof(resp), &dll_seq);
    if (ret != STATUS_NOERROR) {
        log_msg("<< StopMsgFilter -> %s (%.1fms)", error_name(ret), get_time_ms() - start_ms);
        emit_j2534_call_failed("PassThruStopMsgFilter", dll_seq, MSG_STOP_FILTER_REQ, 8, get_time_ms() - start_ms, ret, "transport_failed");
        return ret;
    }

    if (resp_len >= 4) {
        long rc = read_uint32_be(resp);
        log_msg("<< StopMsgFilter -> %s (%.1fms)", error_name(rc), get_time_ms() - start_ms);
        if (rc == STATUS_NOERROR) {
            emit_j2534_call_finished("PassThruStopMsgFilter", dll_seq, MSG_STOP_FILTER_REQ, 8, get_time_ms() - start_ms, rc);
        } else {
            emit_j2534_call_failed("PassThruStopMsgFilter", dll_seq, MSG_STOP_FILTER_REQ, 8, get_time_ms() - start_ms, rc, "remote_return_code");
        }
        return rc;
    }

    emit_j2534_call_failed("PassThruStopMsgFilter", dll_seq, MSG_STOP_FILTER_REQ, 8, get_time_ms() - start_ms, ERR_FAILED, "short_response");
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
    unsigned long dll_seq = 0;
    long ret;
    double start_ms = get_time_ms();

    if (pFirmwareVersion == NULL || pDllVersion == NULL || pApiVersion == NULL) {
        return ERR_NULL_PARAMETER;
    }

    log_msg(">> PassThruReadVersion(dev=%lu)", DeviceID);
    emit_j2534_call_started("PassThruReadVersion", g_sequence + 1, MSG_READ_VERSION_REQ, 4);

    write_uint32_be(body, DeviceID);

    ret = send_recv(MSG_READ_VERSION_REQ, body, 4, resp, &resp_len, sizeof(resp), &dll_seq);
    if (ret != STATUS_NOERROR) {
        log_msg("<< ReadVersion -> %s (%.1fms)", error_name(ret), get_time_ms() - start_ms);
        emit_j2534_call_failed("PassThruReadVersion", dll_seq, MSG_READ_VERSION_REQ, 4, get_time_ms() - start_ms, ret, "transport_failed");
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
        if (return_code == STATUS_NOERROR) {
            emit_j2534_call_finished("PassThruReadVersion", dll_seq, MSG_READ_VERSION_REQ, 4, get_time_ms() - start_ms, return_code);
        } else {
            emit_j2534_call_failed("PassThruReadVersion", dll_seq, MSG_READ_VERSION_REQ, 4, get_time_ms() - start_ms, return_code, "remote_return_code");
        }
        return return_code;
    }

    emit_j2534_call_failed("PassThruReadVersion", dll_seq, MSG_READ_VERSION_REQ, 4, get_time_ms() - start_ms, ERR_FAILED, "short_response");
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
    unsigned long dll_seq = 0;
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

    emit_j2534_call_started("PassThruIoctl", g_sequence + 1, MSG_IOCTL_REQ, body_len);
    ret = send_recv(MSG_IOCTL_REQ, body, body_len, resp, &resp_len, sizeof(resp), &dll_seq);
    if (ret != STATUS_NOERROR) {
        log_msg("<< Ioctl(%s) -> %s (%.1fms)", ioctl_name(IoctlID),
                error_name(ret), get_time_ms() - start_ms);
        emit_j2534_call_failed("PassThruIoctl", dll_seq, MSG_IOCTL_REQ, body_len, get_time_ms() - start_ms, ret, "transport_failed");
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

        if (return_code == STATUS_NOERROR) {
            emit_j2534_call_finished("PassThruIoctl", dll_seq, MSG_IOCTL_REQ, body_len, get_time_ms() - start_ms, return_code);
        } else {
            emit_j2534_call_failed("PassThruIoctl", dll_seq, MSG_IOCTL_REQ, body_len, get_time_ms() - start_ms, return_code, "remote_return_code");
        }
        return return_code;
    }

    log_msg("<< Ioctl(%s) -> FAILED (short response, %.1fms)",
            ioctl_name(IoctlID), get_time_ms() - start_ms);
    emit_j2534_call_failed("PassThruIoctl", dll_seq, MSG_IOCTL_REQ, body_len, get_time_ms() - start_ms, ERR_FAILED, "short_response");
    return ERR_FAILED;
}
