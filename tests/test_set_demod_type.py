"""set_demod_type()'s range check must derive from DemodType.N_DEMOD
(audit F13).  types.py's 2026-08 resync added IDLE_DEMOD=5 / N_DEMOD=6;
the old hardcoded ``<= 4`` incorrectly rejects IDLE_DEMOD today."""

import threading
import time
from unittest.mock import MagicMock

import pytest

from ka9q.control import RadiodControl
from ka9q.exceptions import ValidationError
from ka9q.types import DemodType


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


def test_accepts_every_valid_demod_including_idle():
    c = _bare_control()
    c.send_command = MagicMock()
    for demod in range(DemodType.N_DEMOD):        # 0..5 today
        c.set_demod_type(ssrc=12345, demod_type=demod)
    assert c.send_command.call_count == DemodType.N_DEMOD


def test_rejects_n_demod_and_negative():
    c = _bare_control()
    c.send_command = MagicMock()
    with pytest.raises(ValidationError, match="demod_type"):
        c.set_demod_type(ssrc=12345, demod_type=DemodType.N_DEMOD)
    with pytest.raises(ValidationError, match="demod_type"):
        c.set_demod_type(ssrc=12345, demod_type=-1)
    c.send_command.assert_not_called()
