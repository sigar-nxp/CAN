# Test for incoming status frames decoding
from canbus.parser import CANParser
from canbus.protocol import CANFrame

frame = CANFrame(
    arbitration_id=0x201,
    data=bytes([1, 0, 6, 0])
)

parser = CANParser()
msg = parser.parse(frame)

print(msg)