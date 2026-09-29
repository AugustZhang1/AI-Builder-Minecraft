"""Minimal Minecraft RCON client (standard library only)."""
import socket
import struct
import threading


class RconError(Exception):
    pass


class Rcon:
    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout
        self.sock = None
        self.lock = threading.Lock()
        self.next_id = 1

    def _send(self, kind: int, body: str) -> int:
        req_id = self.next_id
        self.next_id += 1
        data = struct.pack("<ii", req_id, kind) + body.encode("utf-8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(data)) + data)
        return req_id

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RconError("connection closed")
            buf += chunk
        return buf

    def _recv(self) -> tuple[int, str]:
        (length,) = struct.unpack("<i", self._recv_exact(4))
        data = self._recv_exact(length)
        req_id, _kind = struct.unpack("<ii", data[:8])
        return req_id, data[8:-2].decode("utf-8", errors="replace")

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        req_id = self._send(3, self.password)
        got_id, _ = self._recv()
        if got_id != req_id:
            self.close()
            raise RconError("RCON login failed (wrong password?)")

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    def command(self, cmd: str) -> str:
        """Run one command and return the reply. Reconnects once if the link dropped."""
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.sock is None:
                        self.connect()
                    req_id = self._send(2, cmd)
                    got_id, body = self._recv()
                    if got_id != req_id:
                        raise RconError(f"unexpected reply id {got_id}")
                    return body
                except (OSError, RconError):
                    self.close()
                    if attempt == 2:
                        raise
