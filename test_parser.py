from canbus.parser import CANParser, CANFrame
import canbus.constants as constants

parser = CANParser()

# Test part 1 (4 bytes of payload, plus 4 bytes header)
frame1 = CANFrame(arbitration_id=constants.CAN_STATUS | 1, data=bytes([constants.STATUS_WL_LIST_V2_UID_PART1, 0, 0, 1, 0xaa, 0xbb, 0xcc, 0xdd]))
res1 = parser.parse(frame1)
print("Part 1:", res1)

# Test part 2 (2 bytes of payload, plus 4 bytes header) -> length 6
frame2 = CANFrame(arbitration_id=constants.CAN_STATUS | 1, data=bytes([constants.STATUS_WL_LIST_V2_UID_PART2, 0, 0, 1, 0xee, 0xff]))
res2 = parser.parse(frame2)
print("Part 2 (len 6):", res2)

