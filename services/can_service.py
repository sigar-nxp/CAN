"""
PS Locks OIP
CAN Service

Low-Level Zugriff auf SocketCAN (python-can)
"""

import can

from .protocol import CANFrame


class CANService:
    """
    Low-Level CAN Service
    """

    def __init__(
        self,
        channel: str = "can0",
        bitrate: int = 50000,
    ):

        self.channel = channel
        self.bitrate = bitrate

        self.bus = can.interface.Bus(
            interface="socketcan",
            channel=self.channel,
            bitrate=self.bitrate,
        )

    # ---------------------------------------------------------

    def send(self, frame: CANFrame) -> None:
        """
        Sendet ein CAN-Telegramm.
        """

        msg = can.Message(
            arbitration_id=frame.arbitration_id,
            data=frame.data,
            is_extended_id=False,
        )

        self.bus.send(msg)

    # ---------------------------------------------------------

    def receive(self, timeout: float = 1.0):
        """
        Empfängt ein CAN-Telegramm.
        """

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
        """
        Schließt den CAN-Bus.
        """

        self.bus.shutdown()

    # ---------------------------------------------------------

    def __enter__(self):
        return self

    # ---------------------------------------------------------

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
