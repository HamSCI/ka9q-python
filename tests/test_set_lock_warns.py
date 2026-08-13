"""set_lock() must warn loudly: radiod has no LOCK command handler, so the
correct bytes it sends are silently discarded (audit finding F6)."""

import logging
import threading
import time
from unittest.mock import MagicMock

from ka9q.control import RadiodControl


def _bare_control() -> RadiodControl:
    c = RadiodControl.__new__(RadiodControl)
    c.status_address = "test.local"
    c.socket = MagicMock()
    c.dest_addr = ("239.1.2.3", 5006)
    c._socket_lock = threading.RLock()
    c.max_commands_per_sec = 100
    c._command_count = 0
    c._command_window_start = time.time()
    c._rate_limit_lock = threading.Lock()
    c.metrics = MagicMock()
    c._requested_encoding = {}
    return c


def test_set_lock_sends_but_warns(caplog):
    c = _bare_control()
    c.send_command = MagicMock()
    with caplog.at_level(logging.WARNING, logger="ka9q.control"):
        c.set_lock(ssrc=12345, lock=True)
    c.send_command.assert_called_once()   # the (correct) bytes still go out
    assert "no LOCK command handler" in caplog.text
