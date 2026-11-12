"""
Shared pytest configuration.

Runs before any test module imports the application, so the environment set up here
decides which database and locker mode the whole suite uses.
"""

import atexit
import os
import shutil
import tempfile

# Never run the suite against the gateway's real SQLite database: several tests wipe
# the devices table. An explicit DATABASE_URL in the environment still takes precedence.
if "DATABASE_URL" not in os.environ:
    _TEST_DB_DIR = tempfile.mkdtemp(prefix="pslocks_tests_")
    os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TEST_DB_DIR, 'pslocks_test.db')}"
    atexit.register(shutil.rmtree, _TEST_DB_DIR, ignore_errors=True)

# Tests must never transmit on a live SocketCAN bus (a gateway may be running on this host).
os.environ.setdefault("CAN_SIMULATION", "1")

# The gateway defaults to gym mode, which factory-resets any lock seen on node 1 and
# answers card requests itself. Legacy tests use node 1 as an ordinary lock, so the suite
# starts in standard mode; tests/test_gym_locker_mode.py switches to gym mode explicitly.
os.environ.setdefault("LOCKER_OPERATION_MODE", "standard")