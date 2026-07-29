from __future__ import annotations
from unittest.mock import MagicMock, patch

import pytest

from core.sftp_hops import get_hops, add_jump_hop, get_active_sftp, remove_last_hop


class FakeSession:
    def __init__(self, connection):
        self.connection = connection
        self.sftp_hops: list = []
        self.active_hop_index = 0


def _fake_ssh_connection():
    conn = MagicMock()
    conn.label = "SSH"
    conn.host = "gateway.example.com"
    conn.get_sftp.return_value = MagicMock(name="sftp0")
    return conn


def test_get_hops_initializes_first_hop_lazily():
    session = FakeSession(_fake_ssh_connection())
    hops = get_hops(session)
    assert len(hops) == 1
    assert hops[0]["label"] == "Hop 1: gateway.example.com"
    assert session.active_hop_index == 0


def test_get_hops_requires_active_ssh_connection():
    session = FakeSession(connection=None)
    with pytest.raises(RuntimeError):
        get_hops(session)


def test_get_hops_rejects_non_ssh_connection():
    conn = MagicMock()
    conn.label = "Telnet"
    session = FakeSession(conn)
    with pytest.raises(RuntimeError):
        get_hops(session)


def test_get_hops_is_idempotent():
    session = FakeSession(_fake_ssh_connection())
    first = get_hops(session)
    second = get_hops(session)
    assert first is second
    session.connection.get_sftp.assert_called_once()


def test_get_active_sftp_returns_current_hop_sftp():
    session = FakeSession(_fake_ssh_connection())
    sftp = get_active_sftp(session)
    assert sftp is session.connection.get_sftp.return_value


def test_add_jump_hop_tunnels_through_active_transport():
    session = FakeSession(_fake_ssh_connection())
    get_hops(session)  # establish hop 0

    transport = MagicMock()
    transport.is_active.return_value = True
    session.connection.client = MagicMock()
    session.connection.client.get_transport.return_value = transport

    with patch("core.sftp_hops.paramiko.SSHClient") as MockClient:
        new_client = MockClient.return_value
        new_client.open_sftp.return_value = MagicMock(name="sftp1")

        hop = add_jump_hop(session, "10.0.0.9", 22, "bob", "pw")

        transport.open_channel.assert_called_once()
        new_client.connect.assert_called_once()
        assert session.sftp_hops[-1] is hop
        assert session.active_hop_index == 1
        assert "10.0.0.9" in hop["label"]
        assert "bob" in hop["label"]


def test_add_jump_hop_fails_if_transport_inactive():
    session = FakeSession(_fake_ssh_connection())
    get_hops(session)
    transport = MagicMock()
    transport.is_active.return_value = False
    session.connection.client = MagicMock()
    session.connection.client.get_transport.return_value = transport

    with pytest.raises(RuntimeError):
        add_jump_hop(session, "10.0.0.9", 22, "bob", "pw")


def test_add_jump_hop_uses_previous_hop_client_when_chaining():
    session = FakeSession(_fake_ssh_connection())
    get_hops(session)

    first_transport = MagicMock()
    first_transport.is_active.return_value = True
    session.connection.client = MagicMock()
    session.connection.client.get_transport.return_value = first_transport

    with patch("core.sftp_hops.paramiko.SSHClient") as MockClient1:
        MockClient1.return_value.open_sftp.return_value = MagicMock(name="sftp1")
        add_jump_hop(session, "hop2-host", 22, "u2", "p2")

    # Now chain a third hop off the second hop's client, not the original connection.
    second_hop_client = session.sftp_hops[1]["client"]
    second_transport = MagicMock()
    second_transport.is_active.return_value = True
    second_hop_client.get_transport.return_value = second_transport

    with patch("core.sftp_hops.paramiko.SSHClient") as MockClient2:
        MockClient2.return_value.open_sftp.return_value = MagicMock(name="sftp2")
        add_jump_hop(session, "hop3-host", 22, "u3", "p3")

    second_transport.open_channel.assert_called_once()
    assert len(session.sftp_hops) == 3
    assert session.active_hop_index == 2


def test_remove_last_hop_closes_and_pops():
    session = FakeSession(_fake_ssh_connection())
    get_hops(session)
    fake_sftp = MagicMock()
    fake_client = MagicMock()
    session.sftp_hops.append({"label": "Hop 2", "client": fake_client, "sftp": fake_sftp})
    session.active_hop_index = 1

    remove_last_hop(session)

    fake_sftp.close.assert_called_once()
    fake_client.close.assert_called_once()
    assert len(session.sftp_hops) == 1
    assert session.active_hop_index == 0


def test_remove_last_hop_refuses_to_remove_the_only_hop():
    session = FakeSession(_fake_ssh_connection())
    get_hops(session)
    remove_last_hop(session)
    assert len(session.sftp_hops) == 1  # unchanged


def test_remove_last_hop_swallows_close_errors():
    session = FakeSession(_fake_ssh_connection())
    get_hops(session)
    bad_sftp = MagicMock(close=MagicMock(side_effect=Exception("boom")))
    bad_client = MagicMock(close=MagicMock(side_effect=Exception("boom")))
    session.sftp_hops.append({"label": "Hop 2", "client": bad_client, "sftp": bad_sftp})
    remove_last_hop(session)  # must not raise
    assert len(session.sftp_hops) == 1
