"""
Unified Gateway Server.
Binds main port 18888 and optional compatibility port 8080 in a single process.
"""

from __future__ import annotations

import copy
import os
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any, Dict, Optional

from jarvis_gateway import __version__, ip_sync
from jarvis_gateway.asr.manager import ASRManager
from jarvis_gateway.asr.whisper_cli import WhisperCliASR
from jarvis_gateway.asr.whisper_server import WhisperServerASR
from jarvis_gateway.config import validate_config
from jarvis_gateway.llm.client import LLMClientManager
from jarvis_gateway.memory import Memory
from jarvis_gateway.server.board import BoardLink
from jarvis_gateway.server.handler import JarvisHTTPHandler
from jarvis_gateway.server.service import ServiceControl
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
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(32)
        self._lifecycle_lock = threading.Lock()
        self._stop_requested = False
        self._closed = False
        self.serving = threading.Event()
        self.serve_thread_id = None
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        # 在创建线程前限流，防止慢连接无限占用线程。
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def serve_forever(self, poll_interval=0.1):
        with self._lifecycle_lock:
            if self._stop_requested or self._closed:
                return
            self.serve_thread_id = threading.get_ident()
            self.serving.set()
        try:
            super().serve_forever(poll_interval)
        finally:
            with self._lifecycle_lock:
                self.serving.clear()

    def shutdown(self):
        # stop 可能发生在 bind 完成、serve_forever 尚未进入的窗口内。
        # 标记停止，让之后的 serve_forever 直接返回；不要在未启动时死等。
        with self._lifecycle_lock:
            self._stop_requested = True
            active = self.serving.is_set()
            own_thread = self.serve_thread_id == threading.get_ident()
        if active and not own_thread:
            super().shutdown()

    def server_close(self):
        with self._lifecycle_lock:
            self._closed = True
            self._stop_requested = True
        super().server_close()

    def server_bind(self):
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class UnifiedGatewayServer:
    def __init__(self, config: Dict[str, Any], project_root: str = ""):
        config = copy.deepcopy(config)
        env_token = os.environ.get("JARVIS_AUTH_TOKEN", "")
        if env_token:
            config.setdefault("gateway", {})["auth_token"] = env_token
        validate_config(config)
        self.config = config
        self.project_root = project_root

        # Gateway parameters
        self.host = config["gateway"].get("host", "127.0.0.1")
        self.port = int(config["gateway"].get("port", 18888))
        self.auth_token = config["gateway"].get("auth_token", "")
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

        # 开发板地址联动：Web 控制台在此触发串口写入，不需要单独的菜单栏 App
        self.run_dir = Path(project_root or ip_sync.ROOT) / "run"
        self.board = BoardLink(run_dir=self.run_dir)

        # 服务控制：控制台上就能启动/停止/重启、开关登录自启、看日志尾巴。
        self.service = ServiceControl(project_root or ip_sync.ROOT)

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

        # 长期记忆：记住对话上下文，并定期把对话提炼成事实与行为笔记
        mem_cfg = config.get("memory", {}) or {}
        self.memory = Memory(
            os.environ.get("JARVIS_MEMORY_DIR") or mem_cfg.get("dir") or None,
            complete=self._memory_complete,
            max_facts=int(mem_cfg.get("max_facts", 240)),
            persona_chars=int(mem_cfg.get("persona_chars", 1200)),
            every_turns=int(mem_cfg.get("consolidate_every", 6)),
            min_interval_sec=float(mem_cfg.get("min_interval_sec", 20.0)),
            history_turns=int(mem_cfg.get("history_turns", 6)),
            recall_facts=int(mem_cfg.get("recall_facts", 5)),
            enabled=bool(mem_cfg.get("enabled", True)),
        )

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
        self.turn_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._stopping = threading.Event()

    def _memory_complete(self, messages):
        """记忆整理用的旁路补全：独立于对话超时预算，失败只返回空串。"""
        try:
            return self.llm_manager.complete_text(messages, max_tokens=700, temperature=0.2)
        except Exception:
            return ""

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
        self._stopping.set()
        # serve_forever 的 finally 也会调用这里，避免与外部 shutdown 相互等待。
        if not self._stop_lock.acquire(blocking=False):
            return
        try:
            for httpd in (self.legacy_httpd, self.main_httpd):
                if httpd:
                    httpd.shutdown()
                    httpd.server_close()
            if self._legacy_thread and self._legacy_thread.ident != threading.get_ident():
                self._legacy_thread.join(timeout=2)
            self.token_service.stop()
            self.weather_service.stop()
        finally:
            self._stop_lock.release()

    def serve_forever(self):
        """Start listening on main port and optional legacy data port."""
        handler_cls = self.create_handler_class()

        # Bind the main port first: a failure here must not leave background workers running.
        self.main_httpd = GatewayHTTPServer((self.host, self.port), handler_cls)
        try:
            self._serve_bound(handler_cls)
        finally:
            self.stop_services()

    def _serve_bound(self, handler_cls):
        if self._stopping.is_set():
            return
        self.start_services()
        if self._stopping.is_set():
            return

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

        self.main_httpd.serve_forever()
