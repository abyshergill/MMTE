"""Tiny SSH server for the Rust end-to-end tests (needs paramiko).

Usage: python ssh_server.py PORT ROOT_DIR
Login tester / secret. Offers an echo shell, SFTP rooted at ROOT_DIR and direct-tcpip (jump hops).
"""
import os
import socket
import sys
import threading

import paramiko
from paramiko import (SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface, ServerInterface)
from paramiko.common import AUTH_FAILED, AUTH_SUCCESSFUL, OPEN_SUCCEEDED, OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

PORT = int(sys.argv[1])
ROOT = os.path.abspath(sys.argv[2])
HOST_KEY = paramiko.RSAKey.generate(2048)


class Handle(SFTPHandle):
    def stat(self):
        try:
            return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def chattr(self, attr):
        return paramiko.SFTP_OK


class Sftp(SFTPServerInterface):
    def _real(self, path):
        return os.path.join(ROOT, path.lstrip("/"))

    def list_folder(self, path):
        try:
            out = []
            for name in os.listdir(self._real(path)):
                attr = SFTPAttributes.from_stat(os.stat(os.path.join(self._real(path), name)))
                attr.filename = name
                out.append(attr)
            return out
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def stat(self, path):
        try:
            return SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    lstat = stat

    def open(self, path, flags, attr):
        real = self._real(path)
        try:
            binary = getattr(os, "O_BINARY", 0)
            fd = os.open(real, flags | binary, 0o666)
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        if flags & os.O_WRONLY:
            fstr = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            fstr = "a+b" if flags & os.O_APPEND else "r+b"
        else:
            fstr = "rb"
        f = os.fdopen(fd, fstr)
        h = Handle(flags)
        h.filename = real
        h.readfile = f
        h.writefile = f
        return h

    def remove(self, path):
        try:
            os.remove(self._real(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return paramiko.SFTP_OK

    def rename(self, old, new):
        try:
            os.rename(self._real(old), self._real(new))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return paramiko.SFTP_OK

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._real(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return paramiko.SFTP_OK

    def rmdir(self, path):
        try:
            os.rmdir(self._real(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return paramiko.SFTP_OK

    def canonicalize(self, path):
        p = os.path.normpath("/" + path.lstrip("/")) if path not in (".", "") else "/"
        return p


class Server(ServerInterface):
    def __init__(self):
        self.dests = {}

    def check_auth_password(self, user, password):
        return AUTH_SUCCESSFUL if (user, password) == ("tester", "secret") else AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return OPEN_SUCCEEDED

    def check_channel_pty_request(self, *a):
        return True

    def check_channel_window_change_request(self, *a):
        return True

    def check_channel_shell_request(self, channel):
        threading.Thread(target=shell, args=(channel,), daemon=True).start()
        return True

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        self.dests[chanid] = destination
        return OPEN_SUCCEEDED


def shell(ch):
    ch.send(b"Welcome to test-ssh\r\nroot@ssh:~# ")
    buf = b""
    while True:
        try:
            data = ch.recv(1024)
        except Exception:
            return
        if not data:
            return
        for b in data:
            if b in (13, 10):
                cmd = buf.decode(errors="replace").strip()
                buf = b""
                ch.send(b"\r\n")
                if cmd == "exit":
                    ch.send(b"logout\r\n")
                    ch.close()
                    return
                elif cmd.startswith("echo "):
                    ch.send(cmd[5:].encode() + b"\r\n")
                elif cmd == "pwd":
                    ch.send(b"/srv/data\r\n")
                elif cmd:
                    ch.send(("sh: " + cmd + ": not found\r\n").encode())
                ch.send(b"root@ssh:~# ")
            elif b in (127, 8):
                if buf:
                    buf = buf[:-1]
                    ch.send(b"\b \b")
            elif b == 21:
                buf = b""
            else:
                buf += bytes([b])
                ch.send(bytes([b]))


def pump(chan, dest):
    try:
        s = socket.create_connection(dest, timeout=10)
    except Exception:
        chan.close()
        return

    def a():
        try:
            while True:
                d = s.recv(65536)
                if not d:
                    break
                chan.sendall(d)
        except Exception:
            pass
        try:
            chan.close()
        except Exception:
            pass

    def b():
        try:
            while True:
                d = chan.recv(65536)
                if not d:
                    break
                s.sendall(d)
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass

    threading.Thread(target=a, daemon=True).start()
    threading.Thread(target=b, daemon=True).start()


def serve_conn(client):
    t = paramiko.Transport(client)
    t.add_server_key(HOST_KEY)
    t.set_subsystem_handler("sftp", SFTPServer, Sftp)
    srv = Server()
    t.start_server(server=srv)
    while t.is_active():
        ch = t.accept(1)
        if ch is not None and ch.get_id() in srv.dests:
            pump(ch, srv.dests[ch.get_id()])


def main():
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", PORT))
    sock.listen(20)
    print("READY", flush=True)
    while True:
        c, _ = sock.accept()
        threading.Thread(target=serve_conn, args=(c,), daemon=True).start()


main()
