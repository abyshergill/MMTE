from __future__ import annotations
import os
import queue
import re
import socket
import threading
import time
import paramiko
import serial

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
        self.client.connect(hostname=self.host, port=self.port, username=self.username or None, password=self.password or None, timeout=self.timeout, look_for_keys=self.look_for_keys, allow_agent=self.allow_agent)
        self.channel = self.client.invoke_shell(term="xterm", width=100, height=30)
        self.channel.settimeout(0.2)
        incoming.put("\r\n[SSH connected with interactive PTY]\r\n")
        threading.Thread(target=self._read_loop, args=(incoming,), daemon=True).start()

    def _read_loop(self, incoming: queue.Queue[str]) -> None:
        while not self.stop_event.is_set():
            try:
                if self.channel is None: break
                if self.channel.recv_ready():
                    data = self.channel.recv(8192)
                    if not data: break
                    incoming.put(data.decode(errors="replace"))
                elif self.channel.exit_status_ready(): break
                else: time.sleep(0.02)
            except Exception as exc:
                if not self.stop_event.is_set(): incoming.put(f"\r\n[SSH read error: {exc}]\r\n")
                break
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
        while not self.stop_event.is_set():
            try:
                if not self.serial_obj or not self.serial_obj.is_open: break
                data = self.serial_obj.read(4096)
                if data: incoming.put(data.decode(errors="replace"))
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
        while not self.stop_event.is_set():
            try:
                if not self.sock: break
                data = self.sock.recv(4096)
                if not data: break
                cleaned = re.sub(rb'\xff[\xfb-\xfe].', b'', data)
                if cleaned: incoming.put(cleaned.decode(errors="replace"))
            except socket.timeout: continue
            except Exception as exc:
                if not self.stop_event.is_set(): incoming.put(f"\n[Telnet read error: {exc}]\n")
                break
        incoming.put("\n[Telnet session closed]\n")

    def send(self, data: str) -> None:
        if not self.sock: raise RuntimeError("Telnet socket is not active")
        self.sock.sendall(data.encode())

    def close(self) -> None:
        self.stop_event.set()
        try:
            if self.sock: self.sock.close()
        except Exception: pass
