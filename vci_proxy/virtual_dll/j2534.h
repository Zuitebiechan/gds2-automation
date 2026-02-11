/**
 * J2534 API Header File
 * SAE J2534-1 Pass-Thru Vehicle Programming API
 */

#ifndef J2534_H
#define J2534_H

// Prevent windows.h from including winsock.h
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif

#include <windows.h>

#ifdef __cplusplus
extern "C" {
#endif

// J2534 Error Codes
#define STATUS_NOERROR              0x00
#define ERR_NOT_SUPPORTED           0x01
#define ERR_INVALID_CHANNEL_ID      0x02
#define ERR_INVALID_PROTOCOL_ID     0x03
#define ERR_NULL_PARAMETER          0x04
#define ERR_INVALID_IOCTL_VALUE     0x05
#define ERR_INVALID_FLAGS           0x06
#define ERR_FAILED                  0x07
#define ERR_DEVICE_NOT_CONNECTED    0x08
#define ERR_TIMEOUT                 0x09
#define ERR_INVALID_MSG             0x0A
#define ERR_INVALID_TIME_INTERVAL   0x0B
#define ERR_EXCEEDED_LIMIT          0x0C
#define ERR_INVALID_MSG_ID          0x0D
#define ERR_DEVICE_IN_USE           0x0E
#define ERR_INVALID_IOCTL_ID        0x0F
#define ERR_BUFFER_EMPTY            0x10
#define ERR_BUFFER_FULL             0x11
#define ERR_BUFFER_OVERFLOW         0x12
#define ERR_PIN_INVALID             0x13
#define ERR_CHANNEL_IN_USE          0x14
#define ERR_MSG_PROTOCOL_ID         0x15
#define ERR_INVALID_FILTER_ID       0x16
#define ERR_NO_FLOW_CONTROL         0x17
#define ERR_NOT_UNIQUE              0x18
#define ERR_INVALID_BAUDRATE        0x19
#define ERR_INVALID_DEVICE_ID       0x1A

// Protocol IDs
#define J1850VPW                    0x01
#define J1850PWM                    0x02
#define ISO9141                     0x03
#define ISO14230                    0x04
#define CAN                         0x05
#define ISO15765                    0x06
#define SCI_A_ENGINE                0x07
#define SCI_A_TRANS                 0x08
#define SCI_B_ENGINE                0x09
#define SCI_B_TRANS                 0x0A

// IOCTL IDs
#define GET_CONFIG                  0x01
#define SET_CONFIG                  0x02
#define READ_VBATT                  0x03
#define FIVE_BAUD_INIT              0x04
#define FAST_INIT                   0x05
#define CLEAR_TX_BUFFER             0x07
#define CLEAR_RX_BUFFER             0x08
#define CLEAR_PERIODIC_MSGS         0x09
#define CLEAR_MSG_FILTERS           0x0A
#define CLEAR_FUNCT_MSG_LOOKUP_TABLE 0x0B
#define ADD_TO_FUNCT_MSG_LOOKUP_TABLE 0x0C
#define DELETE_FROM_FUNCT_MSG_LOOKUP_TABLE 0x0D
#define READ_PROG_VOLTAGE           0x0E

// Filter Types
#define PASS_FILTER                 0x01
#define BLOCK_FILTER                0x02
#define FLOW_CONTROL_FILTER         0x03

// Message Flags
#define TX_MSG_TYPE                 0x0001
#define ISO15765_FRAME_PAD          0x0040
#define ISO15765_ADDR_TYPE          0x0080
#define CAN_29BIT_ID                0x0100
#define WAIT_P3_MIN_ONLY            0x0200
#define SW_CAN_HV_TX                0x0400
#define SCI_MODE                    0x400000
#define SCI_TX_VOLTAGE              0x800000

// RX Status Flags
#define TX_DONE                     0x08
#define RX_BREAK                    0x10
#define ISO15765_PADDING_ERROR      0x20
#define ISO15765_FIRST_FRAME        0x02
#define START_OF_MESSAGE            0x02
#define TX_INDICATION               0x08

// PASSTHRU_MSG structure
typedef struct {
    unsigned long ProtocolID;
    unsigned long RxStatus;
    unsigned long TxFlags;
    unsigned long Timestamp;
    unsigned long DataSize;
    unsigned long ExtraDataIndex;
    unsigned char Data[4128];
} PASSTHRU_MSG;

// SCONFIG structure
typedef struct {
    unsigned long Parameter;
    unsigned long Value;
} SCONFIG;

// SCONFIG_LIST structure
typedef struct {
    unsigned long NumOfParams;
    SCONFIG* ConfigPtr;
} SCONFIG_LIST;

// SBYTE_ARRAY structure
typedef struct {
    unsigned long NumOfBytes;
    unsigned char* BytePtr;
} SBYTE_ARRAY;

// J2534 API Function Declarations
#ifdef BUILDING_DLL
#define J2534_API __declspec(dllexport)
#else
#define J2534_API __declspec(dllimport)
#endif

J2534_API long __stdcall PassThruOpen(
    void* pName,
    unsigned long* pDeviceID
);

J2534_API long __stdcall PassThruClose(
    unsigned long DeviceID
);

J2534_API long __stdcall PassThruConnect(
    unsigned long DeviceID,
    unsigned long ProtocolID,
    unsigned long Flags,
    unsigned long BaudRate,
    unsigned long* pChannelID
);

J2534_API long __stdcall PassThruDisconnect(
    unsigned long ChannelID
);

J2534_API long __stdcall PassThruReadMsgs(
    unsigned long ChannelID,
    PASSTHRU_MSG* pMsg,
    unsigned long* pNumMsgs,
    unsigned long Timeout
);

J2534_API long __stdcall PassThruWriteMsgs(
    unsigned long ChannelID,
    PASSTHRU_MSG* pMsg,
    unsigned long* pNumMsgs,
    unsigned long Timeout
);

J2534_API long __stdcall PassThruStartPeriodicMsg(
    unsigned long ChannelID,
    PASSTHRU_MSG* pMsg,
    unsigned long* pMsgID,
    unsigned long TimeInterval
);

J2534_API long __stdcall PassThruStopPeriodicMsg(
    unsigned long ChannelID,
    unsigned long MsgID
);

J2534_API long __stdcall PassThruStartMsgFilter(
    unsigned long ChannelID,
    unsigned long FilterType,
    PASSTHRU_MSG* pMaskMsg,
    PASSTHRU_MSG* pPatternMsg,
    PASSTHRU_MSG* pFlowControlMsg,
    unsigned long* pFilterID
);

J2534_API long __stdcall PassThruStopMsgFilter(
    unsigned long ChannelID,
    unsigned long FilterID
);

J2534_API long __stdcall PassThruSetProgrammingVoltage(
    unsigned long DeviceID,
    unsigned long PinNumber,
    unsigned long Voltage
);

J2534_API long __stdcall PassThruReadVersion(
    unsigned long DeviceID,
    char* pFirmwareVersion,
    char* pDllVersion,
    char* pApiVersion
);

J2534_API long __stdcall PassThruGetLastError(
    char* pErrorDescription
);

J2534_API long __stdcall PassThruIoctl(
    unsigned long ChannelID,
    unsigned long IoctlID,
    void* pInput,
    void* pOutput
);

#ifdef __cplusplus
}
#endif

#endif // J2534_H
