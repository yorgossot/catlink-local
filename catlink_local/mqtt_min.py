"""Minimal MQTT 3.1.1 pieces: the length/string encoders and the Home Assistant side client (HaMqtt).

HaMqtt publishes discovery/state at QoS 0 and subscribes command topics at QoS 0. No third-party deps.
"""
import socket
import threading

from .log import log

MAX_PACKET = 65536


def encode_length(value):
    out = bytearray()
    while True:
        value, digit = divmod(value, 128)
        out.append(digit | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _mqtt_bytes(raw):
    return len(raw).to_bytes(2, "big") + raw


def _mqtt_str(s):
    return _mqtt_bytes(s.encode())


class HaMqtt:
    def __init__(self, host, port, client_id, user, password, keepalive=45, lwt=None, on_message=None):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.user = user
        self.password = password
        self.keepalive = keepalive
        self.lwt = lwt  # (topic, payload, retain)
        self.on_message = on_message
        self.sock = None
        self.tx = threading.Lock()
        self.pid = 0

    def _connect_packet(self):
        payload = _mqtt_str(self.client_id)
        flags = 0x02  # clean session
        if self.lwt:
            topic, msg, retain = self.lwt
            flags |= 0x04 | (0x20 if retain else 0)
            payload += _mqtt_str(topic) + _mqtt_bytes(msg.encode())
        if self.user:
            flags |= 0x80
            payload += _mqtt_str(self.user)
        if self.password is not None:
            flags |= 0x40
            payload += _mqtt_bytes(self.password.encode())
        vh = _mqtt_str("MQTT") + bytes([4, flags]) + self.keepalive.to_bytes(2, "big")
        body = vh + payload
        return b"\x10" + encode_length(len(body)) + body

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=15)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.sendall(self._connect_packet())
        header = self._read(1)[0]
        rest = self._read_remaining()
        if header != 0x20 or len(rest) < 2 or rest[1] != 0:
            raise OSError("ha_connack_refused_%s" % (rest[1] if len(rest) >= 2 else "short"))
        self.sock.settimeout(self.keepalive * 1.5)
        log("ha_connected", host=self.host, port=self.port, user=self.user)

    def publish(self, topic, payload, retain=False):
        if isinstance(payload, str):
            payload = payload.encode()
        body = _mqtt_str(topic) + payload  # QoS 0: no packet id
        header = 0x30 | (1 if retain else 0)
        with self.tx:
            self.sock.sendall(bytes([header]) + encode_length(len(body)) + body)

    def subscribe(self, topic_filter):
        self.pid = (self.pid % 65534) + 1
        body = self.pid.to_bytes(2, "big") + _mqtt_str(topic_filter) + b"\x00"
        with self.tx:
            self.sock.sendall(b"\x82" + encode_length(len(body)) + body)

    def ping(self):
        with self.tx:
            self.sock.sendall(b"\xc0\x00")

    def _read(self, n):
        out = bytearray()
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise OSError("ha_closed")
            out += chunk
        return bytes(out)

    def _read_remaining(self):
        remaining = 0
        for index in range(4):
            value = self._read(1)[0]
            remaining += (value & 0x7F) << (7 * index)
            if not value & 0x80:
                break
        else:
            raise OSError("ha_bad_len")
        if remaining > MAX_PACKET:
            raise OSError("ha_packet_too_large")
        return self._read(remaining) if remaining else b""

    def loop_once(self):
        header = self._read(1)[0]
        body = self._read_remaining()
        kind = header >> 4
        if kind == 3 and self.on_message:  # PUBLISH from the broker
            tlen = int.from_bytes(body[0:2], "big")
            topic = body[2:2 + tlen].decode("utf-8", "replace")
            pos = 2 + tlen
            if (header >> 1) & 3:  # QoS>0 has a packet id; we subscribe at QoS 0 so this is defensive
                pos += 2
            payload = body[pos:].decode("utf-8", "replace")
            # Nothing we publish to a command topic is retained, so a retained PUBLISH on <base>/+/set is
            # a stray replay (e.g. a broker-side retained PRESS). Drop it before dispatch so it can never
            # auto-fire a command after a reconnect/restart.
            if header & 1:
                log("ha_retained_dropped", topic=topic)
                return
            try:
                self.on_message(topic, payload)
            except Exception as exc:  # pragma: no cover - defensive
                log("ha_on_message_error", error=type(exc).__name__)

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
