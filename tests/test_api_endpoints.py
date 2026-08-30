import unittest
from unittest.mock import MagicMock, patch
import can

can.interface.Bus = MagicMock()

from fastapi.testclient import TestClient
from app import app
from config.globals import can_listener, beacon_service

can_listener.start = MagicMock()
beacon_service.start = MagicMock()
can_listener.stop = MagicMock()
beacon_service.stop = MagicMock()

class TestAPIEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_root_endpoint(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["project"], "PS Locks OIP")

    def test_health_endpoint(self):
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "OK")
        self.assertIn("time", response.json())

    def test_version_endpoint(self):
        response = self.client.get("/api/v1/version")
        self.assertEqual(response.status_code, 200)
        self.assertIn("version", response.json())

    def test_info_endpoint(self):
        response = self.client.get("/api/v1/info")
        self.assertEqual(response.status_code, 200)
        self.assertIn("system", response.json())

    @patch("config.globals.can_listener.get_unassigned_devices", return_value=[])
    def test_get_unassigned_devices(self, mock_get):
        response = self.client.get("/api/v1/unassigned")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    @patch("config.globals.can_listener.devices", {})
    def test_get_devices_empty(self):
        response = self.client.get("/api/v1/devices")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    @patch("api.device.lock_service.open")
    def test_open_device(self, mock_open):
        response = self.client.post("/api/v1/devices/5/open")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "opened")
        mock_open.assert_called_once_with(5)

    @patch("api.device.lock_service.open_hold")
    def test_open_hold_device(self, mock_hold):
        response = self.client.post("/api/v1/devices/10/open_hold")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "held open")
        mock_hold.assert_called_once_with(10)

    @patch("api.device.lock_service.open_reset")
    def test_reset_device(self, mock_reset):
        response = self.client.post("/api/v1/devices/15/reset")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "open state reset")
        mock_reset.assert_called_once_with(15)

    @patch("api.device.lock_service.device_reset")
    def test_device_reset(self, mock_device_reset):
        response = self.client.post("/api/v1/devices/20/device_reset")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "device reset")
        mock_device_reset.assert_called_once_with(20)

    @patch("api.device.lock_service.factory_reset")
    def test_factory_reset(self, mock_factory_reset):
        response = self.client.post("/api/v1/devices/25/factory_reset")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "factory reset")
        mock_factory_reset.assert_called_once_with(25)
