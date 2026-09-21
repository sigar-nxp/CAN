"""
PS Locks OIP
CAN API v0.4r4 Constant Definitions.

Contains global constants representing CAN IDs, opcodes, bitrates,
and mask values used for addressing and dispatching packets to devices.
"""

# ==========================================================
# CAN IDs
# ==========================================================

CAN_COMMAND = 0x100
CAN_STATUS = 0x200
CAN_HEALTH = 0x300
CAN_MASTER = 0x400
CAN_ID_REQUEST = 0x500
CAN_UID_PART1 = 0x600
CAN_UID_PART2 = 0x700

# OTA Constants (fitnet_types.h)
FITNET_CAN_ID_OTA_CMD_BASE = 0x780
FITNET_CAN_ID_OTA_ACK_BASE = 0x790
FITNET_CAN_ID_OTA_BASE_MASK = 0x790
FITNET_CAN_ID_OTA_DEVICE_MASK = 0x07F

CMD_OTA_START = 0x01
CMD_OTA_DATA = 0x02
CMD_OTA_VERIFY = 0x03
CMD_OTA_ACTIVATE = 0x04

ACK_OTA_START = 0x81
ACK_OTA_DATA = 0x82
ACK_OTA_VERIFY = 0x83
ACK_OTA_ACTIVATE = 0x84

OTA_DATA_CHUNK_MAX_SIZE = 5

DEVICE_MASK = 0x0FF
TYPE_MASK = 0x700

DEFAULT_DEVICE_ID = 0x7F

# ==========================================================
# COMMANDS
# ==========================================================

OPEN_LOCK = 0x01
REQUEST_STATUS = 0x02
OPEN_HOLD = 0x04
OPEN_RESET = 0x05

LED_SET = 0x40
LED_RESET = 0x41

BUZZ_PLAY = 0x50
BUZZ_STOP = 0x51

REQUEST_FULL_UID = 0xF0

DEVICE_RESET = 0xFA
FACTORY_RESET = 0xFB
REQUEST_HEALTH = 0xFC
REQUEST_VERSION = 0xFD
REQUEST_DEVICE_INFO = 0xFE

# ==========================================================
# LED
# ==========================================================

LED_OFF = 0
LED_GREEN = 1
LED_RED = 2
LED_GREEN_BLINK = 3
LED_RED_BLINK = 4
LED_GREEN_FAST = 5

# ==========================================================
# BUZZER
# ==========================================================

BUZZ_OK = 1
BUZZ_NOT_OK = 2
BUZZ_ERROR = 3
BUZZ_ALARM1 = 4
BUZZ_ALARM2 = 5

# ==========================================================
# STATUS
# ==========================================================

STATUS_BASIC = 0x01
STATUS_COMMAND_ACK = 0x02
STATUS_ERROR = 0xFF

# ==========================================================
# RFID & HEALTH
# ==========================================================
STATUS_RFID_AUTH_REQ_PART2 = 0x14
STATUS_EVENT_WL_AUTO_DELETE = 0x38

HEALTH_SHORT = 0x01
HEALTH_EXTENDED = 0x02
HEALTH_VERSION = 0x03
HEALTH_DEVICE_INFO = 0x04
HEALTH_DIAG_INFO = 0x05
HEALTH_SECURITY_STATUS = 0x06
HEALTH_RUNTIME_STATE = 0x07
HEALTH_DIAG_EXTENDED = 0x08

# ==========================================================
# MASTER
# ==========================================================

MASTER_ASSIGN_ID = 0x01
MASTER_BEACON = 0xB0
# Commands for Whitelist & Config
WL_WRITE_UID = 0x30
WL_DEL = 0x31
WL_CLEAR = 0x32
WL_WRITE_UID_PART2 = 0x33
WL_DEL_PART2 = 0x34
COMMAND_WL_ADD_EPHEMERAL = 0x35
REQUEST_WL_LIST = 0x36
REQUEST_WL_INFO = 0x37
WL_ADD_EPHEMERAL_PART2 = 0x39
SET_LOCK_MODE = 0x3B
REQUEST_LOCK_MODE = 0x3C
DELETE_WL_SLOT = 0x3D
REQUEST_WL_LIST_V2 = 0x3E
REQUEST_OCCUPANCY_STATE = 0x3F
AUTH_RESP = 0x60
SET_WL_SLOT_POLICY = 0x6A

# Status reports for Whitelist & Config
STATUS_WL_INFO_REPORT = 0x37
STATUS_LOCK_MODE_REPORT = 0x3B
STATUS_WL_LIST_V2_ITEM = 0x3D
STATUS_OCCUPANCY_STATE_REPORT = 0x3E
STATUS_WL_LIST_V2_UID_PART1 = 0x40
STATUS_WL_LIST_V2_UID_PART2 = 0x41
STATUS_OCCUPANCY_OWNER_SHORT = 0x42
STATUS_RFID_POLICY_ACTION = 0x43

# ==========================================================
# BITRATES
# ==========================================================

BITRATE_1K = 0
BITRATE_5K = 1
BITRATE_10K = 2
BITRATE_25K = 3
BITRATE_50K = 4
BITRATE_100K = 5
BITRATE_250K = 6
BITRATE_500K = 7
