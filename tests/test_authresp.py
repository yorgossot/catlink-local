"""Loopback TLS test of the bootstrap responder.

Runs the real launcher (`python3 -I -u authresp.py`) as a subprocess on 127.0.0.1:<free port> with a
throwaway self-signed certificate made by the `openssl` CLI in a temp dir. Skipped if `openssl` is
missing. Nothing leaves loopback.
"""
import http.client
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import vectors as v  # noqa: E402

LAUNCHER = os.path.join(ROOT, "authresp.py")
OPENSSL = shutil.which("openssl")
SECRET_MARKER = "sign=NOT-A-REAL-SIGNATURE-7f3a"   # must never reach the log


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def clean_env(**extra):
    env = {k: val for k, val in os.environ.items()
           if not k.startswith("CATLINK_") and k not in ("CREDENTIALS_DIRECTORY", "STATE_DIRECTORY")}
    env.update(extra)
    return env


def client_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


@unittest.skipIf(OPENSSL is None, "openssl CLI not available")
class AuthRespTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="catlink_auth_")
        cls.cert = os.path.join(cls.dir, "cert.pem")
        cls.key = os.path.join(cls.dir, "key.pem")
        subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", cls.key, "-out", cls.cert, "-subj", "/CN=localhost"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def start(self, **extra):
        port = free_port()
        logpath = os.path.join(self.dir, "auth_%d.log" % port)
        env = clean_env(CATLINK_BOX_MAC=v.TEST_MAC, CATLINK_BOX_BIND_IP="127.0.0.1",
                        CATLINK_AUTH_BIND_PORT=str(port), CATLINK_AUTH_CERT=self.cert,
                        CATLINK_AUTH_KEY=self.key, **extra)
        logfh = open(logpath, "w")
        proc = subprocess.Popen([sys.executable, "-I", "-u", LAUNCHER], env=env, stdout=logfh,
                                stderr=subprocess.STDOUT)
        self.addCleanup(self._stop, proc, logfh)
        deadline = time.time() + 10
        while time.time() < deadline:
            if '"event":"start"' in self.read_log(logpath):
                return proc, port, logpath
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        self.fail("authresp did not start: %s" % self.read_log(logpath))

    @staticmethod
    def _stop(proc, logfh):
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        logfh.close()

    @staticmethod
    def read_log(path):
        with open(path) as fh:
            return fh.read()

    def events(self, path):
        out = []
        for line in self.read_log(path).splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def request(self, port, method, path, body=None):
        conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=10, context=client_ctx())
        try:
            headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_bootstrap_devicename_and_404(self):
        proc, port, logpath = self.start(CATLINK_SKIP_PEER_CHECK="1",
                                         CATLINK_ADVERTISED_MQTT_HOST="198.51.100.7",
                                         CATLINK_ADVERTISED_MQTT_PORT="18830")
        body = "productKey=%s&deviceName=%s&%s" % (v.PK, v.DN, SECRET_MARKER)
        for path in ("/auth/bootstrap", "/auth/devicename", "/auth/bootstrap?x=1"):
            status, data = self.request(port, "POST", path, body)
            self.assertEqual(status, 200, path)
            obj = json.loads(data)
            self.assertEqual(obj["code"], 200)
            self.assertEqual(obj["data"]["resources"]["mqtt"], {"host": "198.51.100.7", "port": 18830})
            self.assertEqual((obj["data"]["iotId"], obj["data"]["iotToken"]), ("LOCAL-iotid", "LOCAL-iottoken"))
        status, data = self.request(port, "GET", "/other")
        self.assertEqual((status, data), (404, b""))

        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=10), 0)
        text = self.read_log(logpath)
        names = [e.get("event") for e in self.events(logpath)]
        for name in ("peer_check_disabled", "start", "tcp_accept", "tls_ok", "request", "response", "exit"):
            self.assertIn(name, names)
        requests = [e for e in self.events(logpath) if e.get("event") == "request"]
        self.assertEqual([r["matched"] for r in requests], [True, True, True, False])
        self.assertEqual(requests[2]["path"], "/auth/bootstrap")       # query string stripped
        self.assertNotIn(SECRET_MARKER, text)
        self.assertNotIn(v.PK, text)
        self.assertNotIn(v.DN, text)

    def test_silent_clients_do_not_block_the_next_login(self):
        proc, port, logpath = self.start(CATLINK_SKIP_PEER_CHECK="1")
        # 1) a TCP client that connects and never starts TLS
        silent_tcp = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(silent_tcp.close)
        # 2) a client that completes TLS and then never sends a request (an idle keep-alive)
        silent_tls = client_ctx().wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=5),
                                              server_hostname="localhost")
        self.addCleanup(silent_tls.close)
        started = time.time()
        status, _ = self.request(port, "POST", "/auth/bootstrap", "x=1")
        self.assertEqual(status, 200)
        self.assertLess(time.time() - started, 5, "a silent client delayed the next request")
        self.assertIsNone(proc.poll())

    def test_advertised_defaults_to_box_bind(self):
        proc, port, logpath = self.start(CATLINK_SKIP_PEER_CHECK="1", CATLINK_BOX_BIND_PORT="11883")
        status, data = self.request(port, "POST", "/auth/devicename", "x=1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["data"]["resources"]["mqtt"], {"host": "127.0.0.1", "port": 11883})

    def test_foreign_peer_rejected_and_server_keeps_serving(self):
        proc, port, logpath = self.start(CATLINK_BOX_IP="192.0.2.10")   # documentation range, never us
        for _ in range(2):
            with self.assertRaises((ssl.SSLError, OSError, http.client.HTTPException)):
                self.request(port, "POST", "/auth/bootstrap", "x=1")
        time.sleep(0.3)
        self.assertIsNone(proc.poll())
        names = [e.get("event") for e in self.events(logpath)]
        self.assertEqual(names.count("foreign_peer"), 2)
        self.assertNotIn("tcp_accept", names)
        self.assertNotIn("request", names)
        self.assertNotIn("peer_check_disabled", names)

    def test_missing_certificate_settings_exit_2(self):
        env = clean_env(CATLINK_BOX_MAC=v.TEST_MAC, CATLINK_BOX_BIND_IP="127.0.0.1")
        res = subprocess.run([sys.executable, "-I", LAUNCHER], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(res.returncode, 2)
        self.assertIn("CATLINK_AUTH_CERT", res.stderr)

    def test_unreadable_certificate_exit_2(self):
        env = clean_env(CATLINK_BOX_MAC=v.TEST_MAC, CATLINK_BOX_BIND_IP="127.0.0.1",
                        CATLINK_AUTH_CERT=os.path.join(self.dir, "absent.pem"), CATLINK_AUTH_KEY=self.key,
                        CATLINK_AUTH_BIND_PORT=str(free_port()))
        res = subprocess.run([sys.executable, "-I", LAUNCHER], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(res.returncode, 2)
        self.assertIn("CATLINK_AUTH_CERT", res.stderr)

    def test_credentials_directory_default(self):
        cred = tempfile.mkdtemp(prefix="catlink_cred_", dir=self.dir)
        shutil.copy(self.cert, os.path.join(cred, "cert.pem"))
        shutil.copy(self.key, os.path.join(cred, "key.pem"))
        port = free_port()
        env = clean_env(CATLINK_BOX_MAC=v.TEST_MAC, CATLINK_BOX_BIND_IP="127.0.0.1",
                        CATLINK_AUTH_BIND_PORT=str(port), CATLINK_SKIP_PEER_CHECK="1",
                        CREDENTIALS_DIRECTORY=cred)
        proc = subprocess.Popen([sys.executable, "-I", "-u", LAUNCHER], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        try:
            first = proc.stdout.readline().decode()
            if "peer_check_disabled" in first:
                first = proc.stdout.readline().decode()
            self.assertIn('"event":"start"', first)
        finally:
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=10), 0)
            proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
