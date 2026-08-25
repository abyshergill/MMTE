from __future__ import annotations
import queue
import socket as socket_module
import time
from unittest.mock import MagicMock, patch

import pytest

from core.connections import SSHConnection, SerialConnection, TelnetConnection


# --- SSHConnection ---------------------------------------------------------

def test_ssh_start_invokes_shell_and_reports_connected():
    with patch("core.connections.paramiko.SSHClient") as MockClient:
        client_instance = MockClient.return_value
        channel = MagicMock()
        channel.recv_ready.return_value = False
        channel.exit_status_ready.return_value = True
        client_instance.invoke_shell.return_value = channel

        conn = SSHConnection(host="10.0.0.5", username="admin", password="secret")
        incoming: "queue.Queue[str]" = queue.Queue()
        conn.start(incoming)

        client_instance.connect.assert_called_once()
        _, kwargs = client_instance.connect.call_args
        assert kwargs["hostname"] == "10.0.0.5"
        assert kwargs["username"] == "admin"
        assert kwargs["password"] == "secret"

        msgs = _drain(incoming, expect_at_least=1, timeout=1.0)
        assert any("connecting to 10.0.0.5" in m for m in msgs)
        conn.close()


def test_ssh_send_without_channel_raises():
    conn = SSHConnection(host="h", username="u")
    with pytest.raises(RuntimeError):
        conn.send("data")


def test_ssh_resize_calls_resize_pty_when_channel_present():
    conn = SSHConnection(host="h", username="u")
    conn.channel = MagicMock()
    conn.resize(120, 40)
    conn.channel.resize_pty.assert_called_once_with(width=120, height=40)


def test_ssh_resize_enforces_minimums():
    conn = SSHConnection(host="h", username="u")
    conn.channel = MagicMock()
    conn.resize(1, 1)
    conn.channel.resize_pty.assert_called_once_with(width=20, height=5)


def test_ssh_resize_is_noop_without_channel():
    conn = SSHConnection(host="h", username="u")
    conn.resize(80, 24)  # must not raise


def test_ssh_get_sftp_reuses_client_session():
    conn = SSHConnection(host="h", username="u")
    conn.client = MagicMock()
    first = conn.get_sftp()
    second = conn.get_sftp()
    assert first is second
    conn.client.open_sftp.assert_called_once()


def test_ssh_get_sftp_without_client_raises():
    conn = SSHConnection(host="h", username="u")
    with pytest.raises(RuntimeError):
        conn.get_sftp()


def test_ssh_close_swallows_exceptions_from_each_component():
    conn = SSHConnection(host="h", username="u")
    conn.sftp = MagicMock(close=MagicMock(side_effect=Exception("boom")))
    conn.channel = MagicMock(close=MagicMock(side_effect=Exception("boom")))
    conn.client = MagicMock(close=MagicMock(side_effect=Exception("boom")))
    conn.close()  # must not raise despite every close() failing
    assert conn.stop_event.is_set()


# --- SerialConnection --------------------------------------------------------

def test_serial_send_when_closed_raises():
    conn = SerialConnection(port="COM1", baudrate=9600)
    with pytest.raises(RuntimeError):
        conn.send("AT\r\n")


def test_serial_start_opens_port_and_reports_connected():
    with patch("core.connections.serial.Serial") as MockSerial:
        serial_obj = MockSerial.return_value
        serial_obj.is_open = True
        serial_obj.read.return_value = b""

        conn = SerialConnection(port="/dev/ttyUSB0", baudrate=115200)
        incoming: "queue.Queue[str]" = queue.Queue()
        conn.start(incoming)

        MockSerial.assert_called_once()
        msg = incoming.get(timeout=1.0)
        assert "/dev/ttyUSB0" in msg and "115200" in msg
        conn.close()


def test_serial_close_sets_stop_event_and_closes_port():
    conn = SerialConnection(port="COM1", baudrate=9600)
    conn.serial_obj = MagicMock()
    conn.close()
    assert conn.stop_event.is_set()
    conn.serial_obj.close.assert_called_once()


# --- TelnetConnection --------------------------------------------------------

def test_telnet_send_without_socket_raises():
    conn = TelnetConnection(host="10.0.0.1")
    with pytest.raises(RuntimeError):
        conn.send("data")


def test_telnet_start_creates_connection_and_reports_connected():
    with patch("core.connections.socket.create_connection") as mock_create:
        sock = MagicMock()
        sock.recv.return_value = b""
        mock_create.return_value = sock

        conn = TelnetConnection(host="10.0.0.1", port=23)
        incoming: "queue.Queue[str]" = queue.Queue()
        conn.start(incoming)

        mock_create.assert_called_once_with(("10.0.0.1", 23), timeout=10.0)
        msg = incoming.get(timeout=1.0)
        assert "10.0.0.1:23" in msg
        conn.close()


def test_telnet_strips_iac_sequences():
    conn = TelnetConnection(host="h")
    incoming: "queue.Queue[str]" = queue.Queue()
    # IAC WILL ECHO (\xff\xfb\x01) surrounding plain text.
    raw = b"before" + b"\xff\xfb\x01" + b"after"
    conn.sock = MagicMock()
    conn.sock.recv.side_effect = [raw, socket_module.timeout(), b""]
    conn._read_loop(incoming)
    combined = "".join(_drain(incoming, expect_at_least=1, timeout=1.0))
    assert "before" in combined and "after" in combined
    assert "\xff" not in combined


def test_telnet_close_sets_stop_event():
    conn = TelnetConnection(host="h")
    conn.sock = MagicMock()
    conn.close()
    assert conn.stop_event.is_set()
    conn.sock.close.assert_called_once()


# --- helpers -------------------------------------------------------------------

def _drain(q: "queue.Queue[str]", expect_at_least: int = 1, timeout: float = 1.0) -> list[str]:
    deadline = time.time() + timeout
    out = []
    while time.time() < deadline and len(out) < expect_at_least:
        try:
            out.append(q.get(timeout=0.05))
        except queue.Empty:
            continue
    return out
