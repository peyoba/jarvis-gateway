"""在临时目录编译原生 App 自检入口，不启动 UI 或现有服务。"""

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from jarvis_gateway import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_app_release_metadata_matches():
    plist = plistlib.loads((ROOT / "build/Info.plist").read_bytes())
    project = (ROOT / "pyproject.toml").read_text()
    script = (ROOT / "build_app.sh").read_text()
    assert plist["CFBundleShortVersionString"] == __version__
    assert re.search(r'^version = "([^"]+)"', project, re.M)[1] == __version__
    assert re.search(r'^MIN_MACOS="([^"]+)"', script, re.M)[1] == plist["LSMinimumSystemVersion"]
    assert '-target "${ARCH}-apple-macos${MIN_MACOS}"' in script


@pytest.mark.skipif(sys.platform != "darwin", reason="原生菜单栏程序仅支持 macOS")
def test_native_connection_uses_custom_port_and_resolved_auth(tmp_path):
    compiler = shutil.which("xcrun")
    assert compiler, "macOS 原生回归需要 Xcode Command Line Tools"
    executable = tmp_path / "JarvisMenu"
    build = subprocess.run([
        compiler, "swiftc", "-swift-version", "5",
        "-module-cache-path", str(tmp_path / "modulecache"),
        str(ROOT / "build/main.swift"), "-o", str(executable),
    ], capture_output=True, text=True, timeout=120)
    assert build.returncode == 0, build.stderr
    # 可执行文件的相邻测试工程优先于真实 checkout，回归失败也不探测现有服务。
    (tmp_path / "jarvis").touch()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("X-Jarvis-Token")))
            body = b'{"gateway":{"online":true}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        for raw, extra, expected in (
            ("test-plain", {}, "test-plain"),
            ("${JARVIS_NATIVE_TEST}", {"JARVIS_NATIVE_TEST": "test-expanded"}, "test-expanded"),
            ("test-file", {"JARVIS_AUTH_TOKEN": "test-override"}, "test-override"),
        ):
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"gateway": {
                "port": httpd.server_port, "auth_token": raw,
            }}), encoding="utf-8")
            result = subprocess.run(
                [str(executable), "--selftest", "--config", str(config)],
                env={**os.environ, **extra}, capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, result.stderr
            assert f"gateway_port={httpd.server_port}" in result.stdout
            assert "status=ready" in result.stdout
            assert expected not in result.stdout
            assert requests[-1] == ("/status?summary=1", expected)
        selected = tmp_path / "selected.json"
        selected.write_text(json.dumps({"gateway": {
            "port": httpd.server_port, "auth_token": "test-selected",
        }}), encoding="utf-8")
        environment = {**os.environ, "JARVIS_CONFIG": str(selected)}
        result = subprocess.run(
            [str(executable), "--selftest"], env=environment,
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert requests[-1] == ("/status?summary=1", "test-selected")
        # 显式路径覆盖环境变量，同一选择同时供菜单轮询和自检使用。
        result = subprocess.run(
            [str(executable), "--selftest", "--config", str(config)],
            env=environment, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert requests[-1] == ("/status?summary=1", "test-file")
        before = len(requests)
        environment["JARVIS_CONFIG"] = str(tmp_path / "missing.json")
        result = subprocess.run(
            [str(executable), "--selftest"], env=environment,
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 1
        assert len(requests) == before  # 缺失显式配置不能悄悄访问默认服务。
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
