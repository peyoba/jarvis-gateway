"""
Unified Gateway Server.
Binds main port 18888 and optional compatibility port 8080 in a single process.
"""

from __future__ import annotations

import os
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any, Dict, Optional

from jarvis_gateway import __version__
from jarvis_gateway.asr.manager import ASRManager
from jarvis_gateway.asr.whisper_cli import WhisperCliASR
from jarvis_gateway.asr.whisper_server import WhisperServerASR
from jarvis_gateway.llm.client import LLMClientManager
from jarvis_gateway.server.handler import JarvisHTTPHandler
from jarvis_gateway.services.token_service import TokenService
from jarvis_gateway.services.weather_service import WeatherService
from jarvis_gateway.state import GatewayState
from jarvis_gateway.tts.mac_say import MacSayTTS


class GatewayHTTPServer(ThreadingHTTPServer):
    """HTTP server that never blocks startup on a reverse-DNS lookup.

    ``HTTPServer.server_bind`` calls ``socket.getfqdn()``, which can stall for
    tens of seconds on machines with slow or unreachable DNS. The server name
    is only informational here, so bind directly with the configured host.
    """

    daemon_threads = True

    def server_bind(self):
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class UnifiedGatewayServer:
    def __init__(self, config: Dict[str, Any], project_root: str = ""):
        self.config = config
        self.project_root = project_root

        # Gateway parameters
        self.host = config["gateway"].get("host", "127.0.0.1")
        self.port = int(config["gateway"].get("port", 18888))
        self.auth_token = os.environ.get("JARVIS_AUTH_TOKEN") or config["gateway"].get("auth_token", "")
        self.allowed_hosts = config["gateway"].get("allowed_hosts", []) or []
        self.enable_legacy_data_port = bool(config["gateway"].get("enable_legacy_data_port", False))
        self.data_port = int(config.get("data_service", {}).get("port", 8080))

        # Typing parameters
        typing_cfg = config.get("typing", {})
        self.polish_threshold = float(typing_cfg.get("polish_threshold_sec", 2.8))
        default_mode = str(typing_cfg.get("default_mode", "auto"))
        speak_typed = bool(typing_cfg.get("speak_typed", False))

        # Runtime State
        self.state = GatewayState(default_mode=default_mode, speak_typed=speak_typed, root_dir=project_root)

        # Whisper ASR
        wh_cfg = config.get("whisper", {})
        self.whisper_port = int(wh_cfg.get("port", 18889))
        server_asr = WhisperServerASR(
            port=self.whisper_port,
            host="127.0.0.1",
            language=wh_cfg.get("language", "zh"),
            prompt=wh_cfg.get("prompt", ""),
            beam_size=int(wh_cfg.get("beam_size", 0)),
        )
        cli_asr = WhisperCliASR(
            cli_bin=wh_cfg.get("cli_bin", ""),
            model_path=wh_cfg.get("model", ""),
            language=wh_cfg.get("language", "zh"),
            prompt=wh_cfg.get("prompt", ""),
            beam_size=int(wh_cfg.get("beam_size", 2)),
        )
        self.asr_manager = ASRManager(server_asr=server_asr, cli_fallback_asr=cli_asr)

        # TTS
        tts_cfg = config.get("tts", {})
        self.tts = MacSayTTS(
            voice_name=tts_cfg.get("voice", "Tingting"),
            gain=float(tts_cfg.get("gain", 1.3)),
        )

        # LLM
        self.llm_manager = LLMClientManager(config.get("llm", {}))

        # Background Services
        ds_cfg = config.get("data_service", {})
        self.token_service = TokenService(opentoken_bin=ds_cfg.get("opentoken_bin"))
        self.weather_service = WeatherService(
            city=ds_cfg.get("weather_city", "Beijing"),
            city_label=ds_cfg.get("weather_label", "北京"),
        )

        # Servers
        self.main_httpd: Optional[ThreadingHTTPServer] = None
        self.legacy_httpd: Optional[ThreadingHTTPServer] = None
        self._legacy_thread: Optional[threading.Thread] = None

    def is_whisper_online(self, timeout: float = 0.25) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.whisper_port), timeout=timeout):
                return True
        except Exception:
            return False

    def render_dashboard(self) -> str:
        """Find and read dashboard HTML template."""
        # 1. Check package internal web template
        pkg_web = Path(__file__).parent.parent / "web" / "dashboard.html"
        if pkg_web.is_file():
            return pkg_web.read_text(encoding="utf-8")

        # 2. Legacy location kept for older checkouts
        root_web = Path(self.project_root) / "app" / "dashboard.html"
        if root_web.is_file():
            return root_web.read_text(encoding="utf-8")

        return "<h1>Jarvis Gateway Online</h1><p>Dashboard HTML not found.</p>"

    def create_handler_class(self):
        """Create handler class with bound server instance reference."""
        server_instance = self

        class BoundHandler(JarvisHTTPHandler):
            gateway_server = server_instance

        return BoundHandler

    def start_services(self):
        self.token_service.start()
        self.weather_service.start()

    def stop_services(self):
        self.token_service.stop()
        self.weather_service.stop()
        if self.legacy_httpd:
            try:
                self.legacy_httpd.shutdown()
            except Exception:
                pass
        if self.main_httpd:
            try:
                self.main_httpd.shutdown()
            except Exception:
                pass

    def serve_forever(self):
        """Start listening on main port and optional legacy data port."""
        handler_cls = self.create_handler_class()

        # Bind the main port first: a failure here must not leave background workers running.
        self.main_httpd = GatewayHTTPServer((self.host, self.port), handler_cls)
        self.start_services()

        # Start legacy port listener if enabled and distinct
        if self.enable_legacy_data_port and self.data_port and self.data_port != self.port:
            try:
                self.legacy_httpd = GatewayHTTPServer((self.host, self.data_port), handler_cls)
                self._legacy_thread = threading.Thread(
                    target=self.legacy_httpd.serve_forever,
                    daemon=True,
                    name=f"LegacyPort-{self.data_port}",
                )
                self._legacy_thread.start()
                print(f"📡 兼容数据端口已启动: http://{self.host}:{self.data_port}", flush=True)
            except Exception as e:
                print(f"[!] 绑定兼容数据端口 :{self.data_port} 失败 ({e})，将仅在主端口提供数据接口", flush=True)

        print("=" * 65)
        print(f"🚀 Jarvis 物理语音输入打字中枢已启动 (v{__version__})")
        print(f"📡 监听主地址: http://{self.host}:{self.port}")
        print(f"🖥️ 浏览器控制台: http://127.0.0.1:{self.port}/")
        print(f"🎙️ ASR: whisper-server :{self.whisper_port}")
        print(f"✨ 主模型: {self.config.get('llm', {}).get('primary', {}).get('name', '未命名')}")
        print(f"✨ 备模型: {self.config.get('llm', {}).get('fallback', {}).get('name', '未命名')}")
        print(f"⌨️ 模式分流: 当前模式 [{self.state.mode}]，长短按阈值 {self.polish_threshold:g}s")
        if self.auth_token:
            print("🔒 访问鉴权: 已开启 (X-Jarvis-Token / Bearer Auth)")
        else:
            local_only = self.host in ("127.0.0.1", "localhost", "::1")
            if local_only:
                print("🔒 访问范围: 仅本机 (设置 gateway.host = 0.0.0.0 可开放局域网)")
            else:
                print("⚠️ 访问鉴权: 未配置，且已监听非本机地址！局域网内任意设备均可调用 /chat。")
        print("=" * 65)

        try:
            self.main_httpd.serve_forever()
        finally:
            self.stop_services()
