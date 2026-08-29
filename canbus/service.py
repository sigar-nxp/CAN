"""
PS Locks OIP CAN Service Layer.

Provides low-level access to the SocketCAN interface (via python-can),
handling the setup, connection, transmission, and reception of raw frames
to and from the CAN hardware bus.
"""

import can

from .protocol import CANFrame


class CANService:

    def __init__(
        self,
        channel: str = "can0",
        bitrate: int = 50000,
        simulation: bool = False,
    ):

        self.channel = channel
        self.bitrate = bitrate
        self.simulation = simulation

        self.bus = None

        if not self.simulation:
            self.connect()

    # ---------------------------------------------------------

    def connect(self):

        self.bus = can.interface.Bus(
            interface="socketcan",
            channel=self.channel,
            bitrate=self.bitrate,
        )

    # ---------------------------------------------------------

    def send(self, frame: CANFrame):

        if self.simulation:

            print(
                f"TX  {frame.arbitration_id:03X}   "
                + " ".join(f"{b:02X}" for b in frame.data)
            )
            return

        msg = can.Message(
            arbitration_id=frame.arbitration_id,
            data=frame.data,
            is_extended_id=False,
        )

        self.bus.send(msg)

    # ---------------------------------------------------------

    def receive(self, timeout: float = 1.0):

        if self.simulation:
            return None

        msg = self.bus.recv(timeout)

        if msg is None:
            return None

        return CANFrame(
            arbitration_id=msg.arbitration_id,
            data=bytes(msg.data),
        )

    # ---------------------------------------------------------

    def flush(self):
        """
        Clears the receive buffer.
        (Disabled: Frames are now processed by the CANListener)
        """
        pass

    # ---------------------------------------------------------

    def shutdown(self):

        if self.bus is not None:
            self.bus.shutdown()

    # ---------------------------------------------------------

    def __enter__(self):
        return self

    # ---------------------------------------------------------

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
