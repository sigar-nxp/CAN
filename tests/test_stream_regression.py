import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import Request
from fastapi.responses import StreamingResponse

from api.stream import event_stream, extract_unassigned_devices, stream
from canbus.listener import UnassignedDevice
from config.globals import can_listener


class TestStreamUnassignedRegression(unittest.IsolatedAsyncioTestCase):
    """
    Regression test suite verifying that event_stream() and /api/v1/stream
    handle unassigned locks present in the service context without raising AttributeError.
    """

    def setUp(self):
        self.mock_request = MagicMock(spec=Request)
        self.mock_request.is_disconnected = AsyncMock(return_value=False)

    def test_extract_unassigned_devices_various_formats(self):
        """Verify extraction handles dict of dicts, dict of objects, lists, sets, and strings."""
        # 1. Dict of dicts (format returned by can_listener.get_unassigned_devices())
        dict_of_dicts = {
            "28005800": {
                "uid32_hex": "28005800",
                "last_seen": 100.5,
                "full_uid_hex": None,
            }
        }
        extracted = extract_unassigned_devices(dict_of_dicts)
        self.assertIn("28005800", extracted)
        last_seen, payload = extracted["28005800"]
        self.assertEqual(last_seen, 100.5)
        self.assertEqual(payload["uid32_hex"], "28005800")
        self.assertEqual(payload["uid32"], int("28005800", 16))

        # 2. Dict of UnassignedDevice objects
        dev_obj = UnassignedDevice(bytes.fromhex("28005800"))
        dict_of_objs = {"28005800": dev_obj}
        extracted = extract_unassigned_devices(dict_of_objs)
        self.assertIn("28005800", extracted)
        last_seen, payload = extracted["28005800"]
        self.assertEqual(last_seen, dev_obj.last_seen)
        self.assertEqual(payload["uid32_hex"], "28005800")

        # 3. List of UnassignedDevice objects
        list_of_objs = [dev_obj]
        extracted = extract_unassigned_devices(list_of_objs)
        self.assertIn("28005800", extracted)

        # 4. List of dicts
        list_of_dicts = [{"uid32_hex": "AABBCCDD", "last_seen": 200.0}]
        extracted = extract_unassigned_devices(list_of_dicts)
        self.assertIn("AABBCCDD", extracted)
        last_seen, payload = extracted["AABBCCDD"]
        self.assertEqual(last_seen, 200.0)
        self.assertEqual(payload["uid32"], int("AABBCCDD", 16))

        # 5. Set of hex strings
        set_of_strings = {"11223344", "55667788"}
        extracted = extract_unassigned_devices(set_of_strings)
        self.assertIn("11223344", extracted)
        self.assertIn("55667788", extracted)

        # 6. Dict of timestamps (UID -> timestamp)
        dict_of_timestamps = {"A1B2C3D4": 123456.78}
        extracted = extract_unassigned_devices(dict_of_timestamps)
        self.assertIn("A1B2C3D4", extracted)
        last_seen, payload = extracted["A1B2C3D4"]
        self.assertEqual(last_seen, 123456.78)

        # 7. Empty or None
        self.assertEqual(extract_unassigned_devices({}), {})
        self.assertEqual(extract_unassigned_devices([]), {})
        self.assertEqual(extract_unassigned_devices(None), {})

    async def test_event_stream_with_dict_of_dicts(self):
        """
        Regression test: In event_stream(), current_unassigned is a dict of dicts.
        Ensure no AttributeError: 'str' object has no attribute 'uid32_hex' is raised.
        """
        dict_data = {
            "28005800": {
                "uid32_hex": "28005800",
                "last_seen": 1729000000.0,
                "full_uid_hex": None,
            }
        }

        with patch.object(can_listener, "running", True), \
             patch.object(can_listener, "devices", {}), \
             patch.object(can_listener, "get_unassigned_devices", return_value=dict_data), \
             patch.object(can_listener, "recent_logs", []):

            gen = event_stream(self.mock_request)
            event = await anext(gen)

            self.assertIn("event: unassigned_update", event)
            data_line = [line for line in event.splitlines() if line.startswith("data:")][0]
            data = json.loads(data_line[len("data:"):].strip())
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["uid32_hex"], "28005800")
            self.assertEqual(data[0]["uid32"], int("28005800", 16))

    async def test_event_stream_with_list_of_objects(self):
        """Verify event_stream() handles a list of UnassignedDevice objects without error."""
        dev = UnassignedDevice(bytes.fromhex("A1B2C3D4"))
        with patch.object(can_listener, "running", True), \
             patch.object(can_listener, "devices", {}), \
             patch.object(can_listener, "get_unassigned_devices", return_value=[dev]), \
             patch.object(can_listener, "recent_logs", []):

            gen = event_stream(self.mock_request)
            event = await anext(gen)

            self.assertIn("event: unassigned_update", event)
            data_line = [line for line in event.splitlines() if line.startswith("data:")][0]
            data = json.loads(data_line[len("data:"):].strip())
            self.assertEqual(data[0]["uid32_hex"], "a1b2c3d4")

    async def test_event_stream_with_set_of_strings(self):
        """Verify event_stream() handles a set of UID strings without error."""
        with patch.object(can_listener, "running", True), \
             patch.object(can_listener, "devices", {}), \
             patch.object(can_listener, "get_unassigned_devices", return_value={"DEADBEEF"}), \
             patch.object(can_listener, "recent_logs", []):

            gen = event_stream(self.mock_request)
            event = await anext(gen)

            self.assertIn("event: unassigned_update", event)
            data_line = [line for line in event.splitlines() if line.startswith("data:")][0]
            data = json.loads(data_line[len("data:"):].strip())
            self.assertEqual(data[0]["uid32_hex"], "DEADBEEF")

    async def test_stream_endpoint_with_live_unassigned_locks(self):
        """
        Verify the /api/v1/stream endpoint handler returns a StreamingResponse
        which yields unassigned lock updates without raising AttributeError.
        """
        dev_uid_bytes = bytes.fromhex("28005800")
        with can_listener.lock:
            can_listener.unassigned_devices["28005800"] = UnassignedDevice(dev_uid_bytes)

        orig_running = can_listener.running
        can_listener.running = True
        try:
            resp = await stream(self.mock_request)
            self.assertIsInstance(resp, StreamingResponse)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.media_type, "text/event-stream")

            unassigned_event = None
            async for ev in resp.body_iterator:
                if "event: unassigned_update" in ev:
                    unassigned_event = ev
                    break
            self.assertIsNotNone(unassigned_event)
            self.assertIn("28005800", unassigned_event)
        finally:
            can_listener.running = orig_running
            with can_listener.lock:
                can_listener.unassigned_devices.pop("28005800", None)

    async def test_event_stream_yields_device_removed_event(self):
        """
        Verify that broadcast_sse_event immediately delivers device_removed
        to active SSE stream subscribers.
        """
        from api.stream import broadcast_sse_event

        with patch.object(can_listener, "running", True), \
             patch.object(can_listener, "devices", {}), \
             patch.object(can_listener, "get_unassigned_devices", return_value={}), \
             patch.object(can_listener, "recent_logs", []):

            gen = event_stream(self.mock_request)
            try:
                # Advance stream to trigger registration
                first_event = await anext(gen)

                # Broadcast device removal
                broadcast_sse_event("device_removed", {"device_id": 99})

                # Next event should be the device_removed event
                second_event = await anext(gen)
                self.assertIn("event: device_removed", second_event)
                self.assertIn('"device_id": 99', second_event)
            finally:
                await gen.aclose()
