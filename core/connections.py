from __future__ import annotations
import codecs
import os
import queue
import socket
import threading
import time
import paramiko
import serial

def _utf8_decoder():
    """Incremental decoder: a multi-byte character split across two reads stays intact."""
    return codecs.getincrementaldecoder("utf-8")(errors="replace")


# Telnet protocol bytes (RFC 854/855)
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
OPT_ECHO, OPT_SGA = 1, 3


class TelnetProtocol:
    """Stateful Telnet IAC parser that survives sequences split across recv() calls.

    ``feed`` returns ``(payload, replies)``: the application bytes with all protocol
    commands removed, and the negotiation answers that must be sent back.  We accept
    the server echoing and suppressing go-ahead (what shells expect) and politely
    refuse every other option, answering each (command, option) pair only once so two
    peers can never get stuck in a negotiation loop.
    """

    def __init__(self) -> None:
        self._state = "data"
        self._cmd = 0
        self._answered: set[tuple[int, int]] = set()

    def feed(self, data: bytes) -> tuple[bytes, bytes]:
        out = bytearray()
        replies = bytearray()
        for byte in data:
            if self._state == "data":
                if byte == IAC:
                    self._state = "iac"
                else:
                    out.append(byte)
            elif self._state == "iac":
                if byte == IAC:  # escaped literal 0xFF
                    out.append(IAC)
                    self._state = "data"
                elif byte in (WILL, WONT, DO, DONT):
                    self._cmd = byte
                    self._state = "option"
                elif byte == SB:
                    self._state = "sub"
                else:  # NOP, GA, AYT ... carry no payload
                    self._state = "data"
            elif self._state == "option":
                replies += self._negotiate(self._cmd, byte)
                self._state = "data"
            elif self._state == "sub":
                if byte == IAC:
                    self._state = "sub_iac"
            elif self._state == "sub_iac":
                self._state = "data" if byte == SE else "sub"
        return bytes(out), bytes(replies)

    def _negotiate(self, cmd: int, option: int) -> bytes:
        key = (cmd, option)
        if key in self._answered or cmd in (WONT, DONT):
            return b""
        self._answered.add(key)
        if cmd == WILL:
            return bytes([IAC, DO if option in (OPT_ECHO, OPT_SGA) else DONT, option])
        return bytes([IAC, WONT, option])  # DO: we offer no options of our own

    @staticmethod
    def escape(data: bytes) -> bytes:
        return data.replace(bytes([IAC]), bytes([IAC, IAC]))


class BaseConnection:
    label = "Connection"
    def start(self, incoming: queue.Queue[str]) -> None:
        raise NotImplementedError
    def send(self, data: str) -> None:
        raise NotImplementedError
    def close(self) -> None:
        raise NotImplementedError
    def resize(self, cols: int, rows: int) -> None:
        pass

class SSHConnection(BaseConnection):
    label = "SSH"
    def __init__(self, host: str, username: str, password: str = "", port: int = 22, timeout: float = 15.0, look_for_keys: bool = True, allow_agent: bool = True):
        self.host = host
        self.username = username
        self.password = password
        self.port = int(port or 22)
        self.timeout = timeout
        self.look_for_keys = look_for_keys
        self.allow_agent = allow_agent
        self.client: paramiko.SSHClient | None = None
        self.channel: paramiko.Channel | None = None
        self.stop_event = threading.Event()
        self.sftp: paramiko.SFTPClient | None = None

    def resize(self, cols: int, rows: int) -> None:
        if not self.channel:
            return
        try:
            self.channel.resize_pty(width=max(cols, 20), height=max(rows, 5))
        except Exception:
            pass

    def start(self, incoming: queue.Queue[str]) -> None:
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        incoming.put(f"\r\n[SSH connecting to {self.host}:{self.port} as {self.username}]\r\n")
        try:
            self.client.connect(hostname=self.host, port=self.port, username=self.username or None, password=self.password or None, timeout=self.timeout, look_for_keys=self.look_for_keys, allow_agent=self.allow_agent)
            self.channel = self.client.invoke_shell(term="xterm", width=100, height=30)
        except BaseException:
            self.close()  # do not leak the half-open client/transport thread on failure
            raise
        self.channel.settimeout(0.2)
        incoming.put("\r\n[SSH connected with interactive PTY]\r\n")
        threading.Thread(target=self._read_loop, args=(incoming,), daemon=True).start()

    def _read_loop(self, incoming: queue.Queue[str]) -> None:
        decoder = _utf8_decoder()
        while not self.stop_event.is_set():
            try:
                if self.channel is None: break
                if self.channel.recv_ready():
                    data = self.channel.recv(8192)
                    if not data: break
                    text = decoder.decode(data)
                    if text: incoming.put(text)
                # The peer may close without ever sending an exit status (dropped
                # connection, `exit` on some devices): also stop on closed/EOF.
                elif self.channel.exit_status_ready() or self.channel.closed or self.channel.eof_received: break
                else: time.sleep(0.02)
            except Exception as exc:
                if not self.stop_event.is_set(): incoming.put(f"\r\n[SSH read error: {exc}]\r\n")
                break
        tail = decoder.decode(b"", final=True)
        if tail: incoming.put(tail)
        incoming.put("\r\n[SSH session closed]\r\n")

    def send(self, data: str) -> None:
        if not self.channel: raise RuntimeError("SSH channel is not active")
        self.channel.send(data)

    def close(self) -> None:
        self.stop_event.set()
        try:
            if self.sftp:
                self.sftp.close()
        except Exception:
            pass
        try:
            if self.channel:
                self.channel.close()
        except Exception:
            pass
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass

    def get_sftp(self) -> paramiko.SFTPClient:
        if not self.client:
            raise RuntimeError("SSH client is not connected")
        if self.sftp is None:
            self.sftp = self.client.open_sftp()
        return self.sftp

class SerialConnection(BaseConnection):
    label = "Serial"
    def __init__(self, port: str, baudrate: int, bytesize: int = 8, parity: str = "N", stopbits: float = 1.0, timeout: float = 0.1):
        self.port = port
        self.baudrate = int(baudrate)
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.timeout = timeout
        self.serial_obj: serial.Serial | None = None
        self.stop_event = threading.Event()

    def start(self, incoming: queue.Queue[str]) -> None:
        self.serial_obj = serial.Serial(port=self.port, baudrate=self.baudrate, bytesize=self.bytesize, parity=self.parity, stopbits=self.stopbits, timeout=self.timeout)
        incoming.put(f"[Serial connected: {self.port} @ {self.baudrate}]\n")
        threading.Thread(target=self._read_loop, args=(incoming,), daemon=True).start()

    def _read_loop(self, incoming: queue.Queue[str]) -> None:
        decoder = _utf8_decoder()
        while not self.stop_event.is_set():
            try:
                if not self.serial_obj or not self.serial_obj.is_open: break
                data = self.serial_obj.read(4096)
                if data:
                    text = decoder.decode(data)
                    if text: incoming.put(text)
            except Exception as exc:
                if not self.stop_event.is_set(): incoming.put(f"\n[Serial read error: {exc}]\n")
                break
        incoming.put("\n[Serial session closed]\n")

    def send(self, data: str) -> None:
        if not self.serial_obj or not self.serial_obj.is_open: raise RuntimeError("Serial port is not open")
        self.serial_obj.write(data.encode()); self.serial_obj.flush()

    def close(self) -> None:
        self.stop_event.set()
        try:
            if self.serial_obj: self.serial_obj.close()
        except Exception: pass

class TelnetConnection(BaseConnection):
    label = "Telnet"
    def __init__(self, host: str, port: int = 23, timeout: float = 10.0):
        self.host = host
        self.port = int(port or 23)
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.stop_event = threading.Event()

    def start(self, incoming: queue.Queue[str]) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(0.2)
        incoming.put(f"[Telnet connected to {self.host}:{self.port}]\n")
        threading.Thread(target=self._read_loop, args=(incoming,), daemon=True).start()

    def _read_loop(self, incoming: queue.Queue[str]) -> None:
        decoder = _utf8_decoder()
        protocol = TelnetProtocol()
        while not self.stop_event.is_set():
            try:
                if not self.sock: break
                data = self.sock.recv(4096)
                if not data: break
                payload, replies = protocol.feed(data)
                if replies:
                    self.sock.sendall(replies)
                text = decoder.decode(payload)
                if text: incoming.put(text)
            except socket.timeout: continue
            except Exception as exc:
                if not self.stop_event.is_set(): incoming.put(f"\n[Telnet read error: {exc}]\n")
                break
        incoming.put("\n[Telnet session closed]\n")

    def send(self, data: str) -> None:
        if not self.sock: raise RuntimeError("Telnet socket is not active")
        self.sock.sendall(TelnetProtocol.escape(data.encode()))

    def close(self) -> None:
        self.stop_event.set()
        try:
            if self.sock: self.sock.close()
        except Exception: pass
