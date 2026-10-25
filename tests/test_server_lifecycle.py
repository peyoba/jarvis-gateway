"""服务尚未进入请求循环时停止，也必须退出并释放端口。"""

import copy
import socket
import threading
from http.server import BaseHTTPRequestHandler

from jarvis_gateway.config import DEFAULT_CONFIG
from jarvis_gateway.server.server import GatewayHTTPServer, UnifiedGatewayServer


def assert_port_reusable(port):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


def test_shutdown_before_serve_forever_is_safe():
    httpd = GatewayHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = httpd.server_port
    httpd.shutdown()
    httpd.server_close()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert_port_reusable(port)


def test_shutdown_during_service_startup_is_safe():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        cfg["gateway"]["port"] = sock.getsockname()[1]
    gateway = UnifiedGatewayServer(cfg)
    entered, release = threading.Event(), threading.Event()
    errors = []

    def slow_start():
        entered.set()
        assert release.wait(3)

    def run():
        try:
            gateway.serve_forever()
        except Exception as exc:
            errors.append(exc)

    gateway.start_services = slow_start
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert entered.wait(2)
        gateway.stop_services()
    finally:
        release.set()
        thread.join(timeout=3)
    assert not errors
    assert not thread.is_alive()
    gateway.stop_services()  # 重复停止保持幂等。
    assert_port_reusable(cfg["gateway"]["port"])
