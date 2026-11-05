"""Local HTTPS bootstrap responder: stands in for iot-auth-global(-x).aliyuncs.com on the LAN.

At every cold start the box asks the vendor's IoT auth service (HTTPS) which MQTT broker to use. With the
two auth names redirected to this host by DNS, this process answers that request with the address of the
local gateway (CATLINK_ADVERTISED_MQTT_HOST:PORT), so the box bootstraps without the cloud.

SAFETY. This process never opens an outbound connection and has no code that builds one: there is no
socket.create_connection, no urllib, no client. It only accepts connections on the address it is told to
bind, answers the request, and closes. It cannot send anything to the box beyond the HTTP response to the
box's own request. It serves a fixed JSON with placeholder credentials and the local broker's address; it
never proxies to, or reads from, the vendor host.

A foreign peer (not the box's MAC, or not CATLINK_BOX_IP when that is set) is logged and dropped, and the
process keeps serving: a firewall rule should make that unreachable anyway, and a responder the box
depends on must not stay down. systemd Restart=always is the backstop.

It logs method and path only, never request bodies (those carry productKey, deviceName, sign, clientId,
timestamp). It logs the TLS outcome per connection and the response code. Device identifiers are not
logged. Log is JSONL on stdout (journald); CATLINK_LOG_MAX_BYTES can cap it (default 0 = no cap).

Configuration: CATLINK_* environment variables (config.py). Exit codes: 0 on SIGTERM; 2 on a
configuration error or an unloadable certificate/key.
"""
import http.server
import json
import os
import signal
import ssl
import sys
import threading

from . import config as config_mod
from .log import configure as configure_log
from .log import log

KNOWN_PATHS = ("/auth/devicename", "/auth/bootstrap")
IOT_ID = "LOCAL-iotid"
IOT_TOKEN = "LOCAL-iottoken"
CONN_TIMEOUT_S = 15             # per-connection socket timeout (handshake, request, keep-alive idle)


def auth_bytes(host, port):
    """The fixed response, modelled on the Alibaba Link SDK preauth. The device's parser scans for
    "code":200 and the "iotId"/"iotToken"/"host"/"port" tokens anywhere in the body, so this one shape
    satisfies both /auth/devicename (needs iotId+iotToken+host+port) and /auth/bootstrap (needs
    host+port). iotId/iotToken are placeholders; host/port point at the local gateway."""
    obj = {
        "code": 200,
        "message": "success",
        "data": {
            "iotId": IOT_ID,
            "iotToken": IOT_TOKEN,
            "resources": {"mqtt": {"host": host, "port": port}},
        },
    }
    return json.dumps(obj, separators=(",", ":")).encode()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = CONN_TIMEOUT_S    # an idle keep-alive or a stalled request is closed after this
    server_version = "local"
    sys_version = ""

    def _drain_body(self):
        """Read and DISCARD the request body. Never stored, never logged."""
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        remaining = n
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)
        return n

    def _handle(self):
        clen = self._drain_body()
        path = self.path.split("?", 1)[0]
        matched = path in KNOWN_PATHS
        log("request", method=self.command, path=path, content_length=clen, matched=matched)
        if matched:
            body = self.server.auth_body
            self.send_response(200)
            self.send_header("Content-Type", "application/json;charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(body)
            log("response", path=path, code=200, bytes=len(body))
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            log("response", path=path, code=404, bytes=0)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle

    def log_message(self, *a):  # silence the default stderr logging; we log JSONL ourselves
        pass

    def log_error(self, *a):
        pass


class Server(http.server.ThreadingHTTPServer):
    """One thread per connection (the TLS handshake included), each with CONN_TIMEOUT_S on every read and
    write, so a peer that connects and goes silent cannot block the box's next login. Only the pinned
    peer gets past get_request, which bounds the number of threads in practice."""
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, bind, ctx, cfg):
        self.ssl_ctx = ctx
        self.cfg = cfg
        self.auth_body = auth_bytes(cfg.advertised_mqtt_host, cfg.advertised_mqtt_port)
        super().__init__(bind, Handler)

    def get_request(self):
        """Accept and enforce the peer (in the serving thread, fast). A foreign peer is logged and
        dropped; the responder keeps serving."""
        raw, addr = self.socket.accept()
        peer_ip = addr[0]
        if not config_mod.peer_is_box(self.cfg, peer_ip):
            log("foreign_peer", peer=peer_ip)
            try:
                raw.close()
            except OSError:
                pass
            raise OSError("foreign peer rejected")
        log("tcp_accept", peer=peer_ip)
        return raw, addr

    def finish_request(self, request, client_address):
        """Runs in the per-connection thread: TLS-wrap with a timeout, log the outcome, serve, close."""
        peer_ip = client_address[0]
        request.settimeout(CONN_TIMEOUT_S)
        try:
            tls = self.ssl_ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            log("tls_fail", peer=peer_ip, error=type(exc).__name__)
            try:
                request.close()
            except OSError:
                pass
            return
        log("tls_ok", peer=peer_ip, version=tls.version(), cipher=(tls.cipher() or [None])[0])
        try:
            self.RequestHandlerClass(tls, client_address, self)
        finally:
            try:
                tls.close()
            except OSError:
                pass

    def handle_error(self, request, client_address):
        # A failed/rejected/timed-out connection is not fatal and must not print bodies or tracebacks.
        pass


def build_ctx(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    except (ValueError, AttributeError):
        pass
    # Embedded mbedTLS clients may offer only older/RSA ciphers; widen what we'll negotiate.
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    except ssl.SSLError:
        pass
    return ctx


def on_term(_sig, _frame):
    log("exit", reason="sigterm", code=0)
    os._exit(0)


def main():
    try:
        cfg = config_mod.load("authresp")
    except config_mod.ConfigError as exc:
        sys.stderr.write("catlink-authresp: configuration error: %s\n" % exc)
        raise SystemExit(2)
    configure_log(max_bytes=cfg.log_max_bytes)
    try:
        ctx = build_ctx(cfg.auth_cert, cfg.auth_key)
    except (OSError, ssl.SSLError):
        sys.stderr.write("catlink-authresp: cannot load the TLS certificate/key "
                         "(CATLINK_AUTH_CERT / CATLINK_AUTH_KEY)\n")
        raise SystemExit(2)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, on_term)
    if cfg.skip_peer_check:
        log("peer_check_disabled", warning="CATLINK_SKIP_PEER_CHECK=1: ANY peer is accepted; for tests only")
    srv = Server((cfg.auth_bind_ip, cfg.auth_bind_port), ctx, cfg)
    log("start", bind="%s:%d" % (cfg.auth_bind_ip, cfg.auth_bind_port), pid=os.getpid(),
        expect_peer=cfg.box_ip or None, mac_check=not cfg.skip_peer_check,
        advertised="%s:%d" % (cfg.advertised_mqtt_host, cfg.advertised_mqtt_port), paths=list(KNOWN_PATHS))
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("exit", reason="keyboardinterrupt", code=0)


if __name__ == "__main__":
    main()
