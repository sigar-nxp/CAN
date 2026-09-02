import unittest

from canbus.parser import CANParser, StatusBasic, HealthShort, HealthExtended, HealthVersionInfo, HealthDeviceInfo, HealthDiagInfo, HealthSecurityStatus, RuntimeStateSnapshot, IdRequest, UIDPart1, UIDPart2, WlInfoReport, LockModeReport, WlListV2Item, OccupancyStateReport, WlListV2UidPart1, WlListV2UidPart2
from canbus.protocol import CANFrame
from canbus.constants import *

class TestCANParser(unittest.TestCase):
    def setUp(self):
        self.parser = CANParser()

    def test_parse_id_request(self):
        frame = CANFrame(arbitration_id=CAN_ID_REQUEST | 5, data=bytes([0xAA, 0xBB, 0xCC, 0xDD]))
        result = self.parser.parse(frame)
        self.assertIsInstance(result, IdRequest)
        self.assertEqual(result.device_id, 5)
        self.assertEqual(result.uid32, bytes([0xAA, 0xBB, 0xCC, 0xDD]))

    def test_parse_status_basic(self):
        frame = CANFrame(arbitration_id=CAN_STATUS | 10, data=bytes([STATUS_BASIC, 1, 2, 3]))
        result = self.parser.parse(frame)
        self.assertIsInstance(result, StatusBasic)
        self.assertEqual(result.device_id, 10)
        self.assertEqual(result.lock_id, 1)
        self.assertEqual(result.lock_state, 2)
        self.assertEqual(result.lock_error, 3)

    def test_parse_health_short(self):
        frame = CANFrame(arbitration_id=CAN_HEALTH | 15, data=bytes([HEALTH_SHORT, 0x12, 0x34, 0x1E]))
        result = self.parser.parse(frame)
        self.assertIsInstance(result, HealthShort)
        self.assertEqual(result.device_id, 15)
        self.assertEqual(result.vcc_mv, 0x1234)
        self.assertEqual(result.temp_c, 30)

    def test_parse_health_extended(self):
        # Big-endian test payload: 0x0400 = 1024 RAM, 0x00015180 = 86400 uptime
        frame = CANFrame(arbitration_id=CAN_HEALTH | 20, data=bytes([HEALTH_EXTENDED, 0x04, 0x00, 0x00, 0x01, 0x51, 0x80]))
        result = self.parser.parse(frame)
        self.assertIsInstance(result, HealthExtended)
        self.assertEqual(result.device_id, 20)
        self.assertEqual(result.free_ram, 1024)
        self.assertEqual(result.uptime_s, 86400)

    def test_parse_health_version(self):
        frame = CANFrame(arbitration_id=CAN_HEALTH | 25, data=bytes([HEALTH_VERSION, 1, 2, 3]))
        result = self.parser.parse(frame)
        self.assertIsInstance(result, HealthVersionInfo)
        self.assertEqual(result.device_id, 25)
        self.assertEqual(result.version_str, "1.2.3")

    def test_parse_health_device_info(self):
        frame = CANFrame(arbitration_id=CAN_HEALTH | 30, data=bytes([HEALTH_DEVICE_INFO, 0, 0, 5, 2, 0, 0, 0]))
        result = self.parser.parse(frame)
        self.assertIsInstance(result, HealthDeviceInfo)
        self.assertEqual(result.device_id, 30)
        self.assertEqual(result.hw_info_str, "Type:5 Rev:2")
        self.assertEqual(result.door_close_guard_supported, False)

    def test_parse_uid_part1(self):
        frame = CANFrame(arbitration_id=CAN_UID_PART1 | 35, data=bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77]))
        result = self.parser.parse(frame)
        self.assertIsInstance(result, UIDPart1)
        self.assertEqual(result.device_id, 35)
        self.assertEqual(result.uid, bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77]))

    def test_parse_uid_part2(self):
        frame = CANFrame(arbitration_id=CAN_UID_PART2 | 40, data=bytes([0x88, 0x99]))
        result = self.parser.parse(frame)
        self.assertIsInstance(result, UIDPart2)
        self.assertEqual(result.device_id, 40)
        self.assertEqual(result.uid, bytes([0x88, 0x99]))

    def test_parse_wl_info_report(self):
        frame = CANFrame(arbitration_id=CAN_STATUS | 45, data=bytes([STATUS_WL_INFO_REPORT, 5, 50, 1]))
    def test_parse_wl_list_v2_item(self):
        # WlListV2Item: dev_id, slot_index(data[3]), entry_type((data[4]>>4)&0x0F), uid_len(data[4]&0x0F), policy((data[5]>>4)&0x0F), open_action(data[5]&0x0F), ttl_days(data[6])
        frame = CANFrame(arbitration_id=CAN_STATUS | 55, data=bytes([STATUS_WL_LIST_V2_ITEM, 0, 0, 5, (1<<4)|4, (2<<4)|1, 10]))
        result = self.parser.parse(frame)
        self.assertIsInstance(result, WlListV2Item)
        self.assertEqual(result.device_id, 55)
        self.assertEqual(result.slot_index, 5)
        self.assertEqual(result.entry_type, 1)
        self.assertEqual(result.uid_len, 4)
        self.assertEqual(result.policy, 2)
        self.assertEqual(result.open_action, 1)
        self.assertEqual(result.ttl_days, 10)

    def test_parse_wl_list_v2_uid_part1(self):
        frame = CANFrame(arbitration_id=CAN_STATUS | 60, data=bytes([STATUS_WL_LIST_V2_UID_PART1, 0, 0, 3, 0xAA, 0xBB, 0xCC, 0xDD]))
        result = self.parser.parse(frame)
        self.assertIsInstance(result, WlListV2UidPart1)
        self.assertEqual(result.device_id, 60)
        self.assertEqual(result.slot_index, 3)
        self.assertEqual(result.uid_bytes, bytes([0xAA, 0xBB, 0xCC, 0xDD]))

    def test_parse_wl_list_v2_uid_part2(self):
        frame = CANFrame(arbitration_id=CAN_STATUS | 65, data=bytes([STATUS_WL_LIST_V2_UID_PART2, 0, 0, 3, 0xEE, 0xFF, 0x00]))
        result = self.parser.parse(frame)
        self.assertIsInstance(result, WlListV2UidPart2)
        self.assertEqual(result.device_id, 65)
        self.assertEqual(result.slot_index, 3)
        self.assertEqual(result.uid_bytes, bytes([0xEE, 0xFF, 0x00]))

    def test_parse_occupancy_state_report(self):
        frame = CANFrame(arbitration_id=CAN_STATUS | 70, data=bytes([STATUS_OCCUPANCY_STATE_REPORT, 0, 1, 0, 5, 0x01, 0x02, 0x03]))
        result = self.parser.parse(frame)
        self.assertIsInstance(result, OccupancyStateReport)
        self.assertEqual(result.device_id, 70)
        self.assertEqual(result.occupied, True)
        self.assertEqual(result.owner_present, False)
        self.assertEqual(result.source, 5)
        self.assertEqual(result.state_counter_24, 0x010203)

    def test_parse_runtime_state_snapshot(self):
        frame = CANFrame(arbitration_id=CAN_HEALTH | 50, data=bytes([HEALTH_RUNTIME_STATE, 0, 1, 0, 2, 3, 10, 42]))
        result = self.parser.parse(frame)
        self.assertIsInstance(result, RuntimeStateSnapshot)
        self.assertEqual(result.device_id, 50)
        self.assertEqual(result.lock_id, 0)
        self.assertEqual(result.physical_state, 1)
        self.assertEqual(result.lock_error, 0)
        self.assertEqual(result.occupancy_flags, 2)
        self.assertEqual(result.led_status, 3)
        self.assertEqual(result.led_remaining_s, 10)
        self.assertEqual(result.state_revision, 42)
