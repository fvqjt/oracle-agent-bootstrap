"""Pure stdlib WebSocket client for Oracle Agent runner.

No third-party dependencies. Uses socket + ssl + hashlib + base64.
Implements RFC 6455 minimal subset: text frames, masking, ping/pong, close.
Auto-reconnects with exponential backoff (1s -> 30s cap).
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time

# WS opcodes
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def _generate_key() -> str:
    return base64.b64encode(os.urandom(16)).decode()


class WSClient:
    """Minimal WebSocket client (RFC 6455)."""

    def __init__(self, url: str, on_message, on_open=None, on_close=None):
        self.url = url
        self.on_message = on_message  # callback(json_dict)
        self.on_open = on_open        # callback()
        self.on_close = on_close      # callback()

        self._sock = None
        self._connected = False
        self._stop = False
        self._thread = None
        self._send_lock = threading.Lock()

    # -- connection management --

    def is_connected(self) -> bool:
        return self._connected

    def start(self):
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        self._connected = False
        self._close_sock()

    def _run(self):
        backoff = 1
        while not self._stop:
            try:
                self._connect()
                backoff = 1  # reset on success
                if self.on_open:
                    self.on_open()
                self._recv_loop()
            except Exception as e:
                pass
            finally:
                self._connected = False
                self._close_sock()
                if self.on_close:
                    try:
                        self.on_close()
                    except Exception:
                        pass
            if self._stop:
                break
            # exponential backoff
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def _connect(self):
        # Parse URL: ws://host:port/path or wss://host:port/path
        if self.url.startswith("wss://"):
            host_port_path = self.url[6:]
            use_tls = True
        elif self.url.startswith("ws://"):
            host_port_path = self.url[5:]
            use_tls = False
        else:
            raise ValueError(f"bad URL: {self.url}")

        slash = host_port_path.find("/")
        if slash == -1:
            host_port = host_port_path
            path = "/"
        else:
            host_port = host_port_path[:slash]
            path = host_port_path[slash:]

        if ":" in host_port:
            host, port = host_port.rsplit(":", 1)
            port = int(port)
        else:
            host = host_port
            port = 443 if use_tls else 80

        # TCP connect
        raw = socket.create_connection((host, port), timeout=15)

        # TLS wrap
        if use_tls:
            ctx = ssl.create_default_context()
            self._sock = ctx.wrap_socket(raw, server_hostname=host)
        else:
            self._sock = raw

        self._sock.settimeout(None)  # blocking for recv loop

        # WS handshake
        key = _generate_key()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self._sock.sendall(req.encode())

        # Read handshake response
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake: connection closed")
            resp += chunk

        status_line = resp.split(b"\r\n", 1)[0].decode()
        if "101" not in status_line:
            raise ConnectionError(f"handshake failed: {status_line}")

        # Verify Sec-WebSocket-Accept
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if expected.encode() not in resp:
            raise ConnectionError("handshake: bad Sec-WebSocket-Accept")

        self._connected = True

    def _close_sock(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    # -- frame encoding --

    def send_text(self, text: str):
        with self._send_lock:
            self._send_frame(OP_TEXT, text.encode())

    def send_json(self, obj: dict):
        self.send_text(json.dumps(obj))

    def _send_frame(self, opcode: int, payload: bytes):
        if not self._sock:
            return
        # Client must mask
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        header = bytearray()
        header.append(0x80 | opcode)  # FIN + opcode
        plen = len(payload)
        if plen < 126:
            header.append(0x80 | plen)  # mask bit + len
        elif plen < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", plen))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", plen))
        header.extend(mask)
        header.extend(masked)

        self._sock.sendall(bytes(header))

    # -- frame decoding --

    def _recv_loop(self):
        while not self._stop:
            opcode, payload = self._recv_frame()
            if opcode is None:
                break
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
            elif opcode == OP_PONG:
                pass
            elif opcode == OP_CLOSE:
                break
            elif opcode == OP_TEXT:
                try:
                    msg = json.loads(payload.decode())
                    self.on_message(msg)
                except Exception:
                    pass
            elif opcode == OP_BINARY:
                pass

    def _recv_frame(self):
        """Receive one frame. Returns (opcode, payload) or (None, None)."""
        try:
            header = self._recv_exact(2)
        except (OSError, ConnectionError):
            return None, None

        if len(header) < 2:
            return None, None

        b0, b1 = header[0], header[1]
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        plen = b1 & 0x7F

        if plen == 126:
            ext = self._recv_exact(2)
            plen = struct.unpack(">H", ext)[0]
        elif plen == 127:
            ext = self._recv_exact(8)
            plen = struct.unpack(">Q", ext)[0]

        if masked:
            mask = self._recv_exact(4)

        payload = self._recv_exact(plen) if plen > 0 else b""

        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        return opcode, payload

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("connection closed")
            buf.extend(chunk)
        return bytes(buf)
