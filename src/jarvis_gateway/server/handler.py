"""
HTTP Request Handler for Jarvis Gateway.
Unifies audio intake, mode synchronization, status monitoring, and data endpoints.
"""

from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import os
import re
import struct
import time
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from jarvis_gateway import __version__
from jarvis_gateway.actions.injector import CursorInjector
from jarvis_gateway.actions.system import SystemActionExecutor, strip_wake_word
from jarvis_gateway.asr.manager import is_hallucination
from jarvis_gateway.audio.converter import (
    MAX_PCM_BYTES,
    AudioLimitError,
    temporary_wav_file,
    wav_bytes_to_pcm16,
)

if TYPE_CHECKING:
    from jarvis_gateway.server.server import UnifiedGatewayServer


class JarvisHTTPHandler(BaseHTTPRequestHandler):
    """Unified HTTP handler for all Jarvis Gateway requests."""

    # Reference to the container server
    gateway_server: UnifiedGatewayServer

    # WAV 容器最多 8 MiB；解码后的 PCM 另行限制为 120 秒。
    max_body_bytes = 8 * 1024 * 1024
    timeout = 30

    def log_message(self, fmt, *args):
        """Keep the gateway log readable: access logs are opt-in."""
        if os.environ.get("JARVIS_HTTP_LOG"):
            super().log_message(fmt, *args)

    def _client_host(self) -> str:
        raw = self.headers.get("Host", "").strip()
        if not raw:
            return ""
        if raw.startswith("["):
            end = raw.find("]")
            return raw[1:end].lower() if end > 0 else raw.lower()
        if raw.count(":") == 1:
            return raw.rsplit(":", 1)[0].lower()
        return raw.lower()

    def _host_allowed(self) -> bool:
        """Block DNS-rebinding: Host must be localhost, an IP literal, or allowlisted."""
        raw = self.headers.get("Host", "").strip()
        if not raw:
            return True  # HTTP/1.0 clients (ESP32) may omit Host
        hostname = self._client_host()
        if hostname in ("localhost", "localhost.localdomain"):
            return True
        if hostname.endswith(".local"):
            return True  # mDNS name, not reachable through public DNS rebinding
        try:
            ipaddress.ip_address(hostname)
            return True
        except ValueError:
            pass
        allowed = getattr(self.gateway_server, "allowed_hosts", []) or []
        return hostname in [str(h).lower() for h in allowed]

    def _cross_site(self) -> bool:
        """True when a browser tells us the request is cross-site (CSRF guard)."""
        return (self.headers.get("Sec-Fetch-Site", "") or "").lower() == "cross-site"

    def _reject(self, status: int, message: str):
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        """Verify auth token if configured in gateway."""
        token = str(self.gateway_server.auth_token or "")
        if not token:
            return True

        auth_header = self.headers.get("Authorization", "")
        custom_header = self.headers.get("X-Jarvis-Token", "")
        provided = ""
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:].strip()
        elif custom_header:
            provided = custom_header.strip()

        if provided and hmac.compare_digest(provided.encode("utf-8"), token.encode("utf-8")):
            return True

        self._reject(401, "Unauthorized: invalid or missing auth token")
        return False

    def _mutation_allowed(self) -> bool:
        """Guard state-changing requests: valid Host, no cross-site origin, valid auth."""
        if not self._host_allowed():
            self._reject(403, "Forbidden: unexpected Host header")
            return False
        if self._cross_site() or not self._origin_allowed():
            self._reject(403, "Forbidden: request must be same-origin")
            return False
        return self._check_auth()

    def _origin_allowed(self) -> bool:
        # ESP32/CLI 没有 Origin；浏览器有 Origin 时严格比较协议、主机和端口。
        origins = self.headers.get_all("Origin", [])
        if not origins:
            return True
        if len(origins) != 1 or len(self.headers.get_all("Host", [])) != 1:
            return False
        try:
            origin = urlparse(origins[0])
            host = urlparse("http://" + self.headers.get("Host", ""))
            return bool(
                origin.scheme == "http" and origin.hostname
                and origin.hostname == host.hostname
                and (origin.port or 80) == (host.port or 80)
                and not origin.username and not origin.password
                and not origin.path and not origin.query and not origin.fragment
            )
        except ValueError:
            return False

    def _send_json(self, data: dict, status: int = 200, extra_headers: dict | None = None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # No wildcard CORS: /status includes recent dictation text and must not be
        # readable by arbitrary websites the user happens to visit.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        """Reject cross-origin preflight by default; the dashboard is same-origin."""
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        server = self.gateway_server
        state = server.state

        if not self._host_allowed():
            self._reject(403, "Forbidden: unexpected Host header")
            return

        # 1. Web dashboard
        if path in ("/", "/dashboard"):
            html = server.render_dashboard()
            self._send_html(html)
            return

        # 2. System status (contains recent dictation text, so protect it when
        # auth is configured; /ping and /mode?simple=1 stay open for the board).
        if path == "/status":
            summary = qs.get("summary") == ["1"]
            if not summary and not self._check_auth():
                return
            st = state.build_status(
                port=server.port,
                whisper_port=server.whisper_port,
                whisper_online=server.is_whisper_online(),
                data_port=server.data_port,
                legacy_enabled=server.enable_legacy_data_port,
                token_snapshot=server.token_service.get_snapshot(),
                weather_snapshot=server.weather_service.get_snapshot(),
            )
            if summary:
                st = {k: st[k] for k in ("gateway", "whisper", "data", "stats")}
                st["gateway"] = {k: v for k, v in st["gateway"].items() if k != "root"}
            self._send_json(st)
            return

        # 3. Heartbeat
        if path in ("/ping", "/healthz"):
            payload = {
                "status": "ready",
                "agent": "Jarvis-Typing-Companion",
                "version": __version__,
                "mode": state.mode,
                "speak_typed": state.speak_typed,
            }
            # The launcher uses this to detect a gateway from another checkout.
            # Only reveal the local filesystem path to loopback callers.
            if self.client_address[0] in ("127.0.0.1", "::1"):
                payload["root"] = server.project_root
            self._send_json(payload)
            return

        # 4. Mode management
        if path == "/mode":
            if "set" in qs:
                self._send_json(
                    {"error": "method not allowed; use POST /mode"},
                    status=405,
                    extra_headers={"Allow": "GET, POST"},
                )
                return

            if "simple" in qs:
                self._send_json({"current_mode": state.mode})
            else:
                self._send_json({
                    "current_mode": state.mode,
                    "options": ["auto", "raw", "polish", "dialog"],
                    "description": {
                        "auto": f"短按(<{server.polish_threshold:g}s)极速原字打字，长按(>={server.polish_threshold:g}s)AI智能润色",
                        "raw": "强制极速原字直出输入",
                        "polish": "强制AI提炼润色后输入",
                        "dialog": "Jarvis常规智能对话与电脑控制",
                    },
                })
            return

        if path == "/toggle-mode":
            self._send_json(
                {"error": "method not allowed; use POST /toggle-mode"},
                status=405,
                extra_headers={"Allow": "POST"},
            )
            return

        # 5. Token & Weather data endpoints (merged from 8080 service)
        if path in ("/cursor-status", "/api/v1/cursor-status"):
            self._send_json(server.token_service.get_cursor_status_payload())
            return

        if path in ("/deepseek-balance", "/api/v1/deepseek-balance"):
            self._send_json(server.token_service.get_deepseek_balance_payload())
            return

        if path in ("/deepseek-account-balance", "/api/v1/deepseek-account-balance"):
            # Deprecated: kept only so old clients do not crash. Never fabricate a balance.
            self._send_json({"error": "deprecated", "use": "/api/v1/deepseek-balance"}, status=410)
            return

        if path in ("/weather", "/api/v1/weather"):
            self._send_json(server.weather_service.get_snapshot())
            return

        if path in ("/token-stats", "/api/v1/token-stats"):
            self._send_json(server.token_service.get_snapshot())
            return

        if path in ("/device-status", "/api/v1/device-status"):
            stats = server.token_service.get_snapshot()
            self._send_json({
                "status": "ok",
                "timestamp": int(time.time()),
                "total_tokens": stats.get("total", 0),
            })
            return

        self._send_json({"error": "endpoint not found", "path": self.path}, status=404)

    def do_POST(self):
        try:
            self._do_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            # 不把可能包含正文/凭据的异常对象或 traceback 写入持久日志。
            print("[!] 请求处理失败 (internal_error)", flush=True)
            state = getattr(self.gateway_server, "state", None)
            if state is not None:
                try:
                    state.record_turn("", "", "", state.mode, 0, "error")
                except Exception:
                    pass
            try:
                self._reject(500, "internal server error")
            except Exception:
                pass

    def _handle_mode_post(self, qs):
        if not self._mutation_allowed():
            return
        mode = qs.get("set", [""])[0] or qs.get("mode", [""])[0]
        if not mode:
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                length = 0
            if length > 4096:
                self._reject(413, "request body too large")
                return
            raw = self.rfile.read(length) if length > 0 else b""
            ctype = (self.headers.get("Content-Type") or "").lower()
            if raw:
                if "application/json" in ctype:
                    try:
                        mode = str(json.loads(raw.decode("utf-8")).get("mode", ""))
                    except Exception:
                        self._reject(400, "invalid JSON body")
                        return
                else:
                    mode = parse_qs(raw.decode("utf-8", "replace")).get("mode", [""])[0]
        if not self.gateway_server.state.set_mode(mode):
            self._send_json({"error": "invalid mode", "options": ["auto", "raw", "polish", "dialog"]}, status=400)
            return
        self._send_json({"current_mode": self.gateway_server.state.mode, "mode": self.gateway_server.state.mode})

    def _do_post(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if not self._mutation_allowed():
            return
        if path == "/mode":
            self._handle_mode_post(qs)
            return
        if path == "/toggle-mode":
            mode = self.gateway_server.state.toggle_mode()
            self._send_json({"current_mode": mode, "mode": mode})
            return
        if path not in ("/chat", "/type"):
            self._send_json({"error": "endpoint not found"}, status=404)
            return

        # 桌面光标是全局共享资源。拒绝重叠请求（不排队、不先读入音频），
        # 避免后来的短语先上屏及并发解码/模型请求的内存放大。
        if not self.gateway_server.turn_lock.acquire(blocking=False):
            self._reject(429, "gateway busy; retry after the current turn completes")
            return
        try:
            self._audio_post(qs)
        finally:
            self.gateway_server.turn_lock.release()

    def _audio_post(self, qs):
        t_req = time.monotonic()
        if self.headers.get("Transfer-Encoding"):
            self._reject(400, "Transfer-Encoding is not supported")
            return
        lengths = self.headers.get_all("Content-Length", [])
        try:
            content_length = int(lengths[0]) if len(lengths) == 1 else 0
        except ValueError:
            content_length = 0
        if content_length <= 0:
            self._reject(411, "Content-Length required")
            return
        if content_length > self.max_body_bytes:
            self._reject(413, f"audio too large (max {self.max_body_bytes} bytes)")
            return
        audio_data = self.rfile.read(content_length)
        if len(audio_data) != content_length or len(audio_data) < 100:
            self._reject(400, "audio too short or incomplete")
            return
        if audio_data[:4] == b"\x1aE\xdf\xa3":
            self._reject(415, "WebM/Opus is not supported; send raw 16 kHz 16-bit mono PCM or RIFF/WAV")
            return
        if audio_data[:4] == b"RIFF":
            try:
                audio_data = wav_bytes_to_pcm16(audio_data)
            except AudioLimitError as exc:
                self._reject(413, str(exc))
                return
            if not audio_data:
                self._reject(400, "unsupported WAV format; expected 16-bit PCM")
                return
        if len(audio_data) > MAX_PCM_BYTES:
            self._reject(413, "decoded audio exceeds 120 seconds")
            return
        if len(audio_data) % 2:
            self._reject(400, "16-bit PCM requires complete samples")
            return
        audio_sec = len(audio_data) / 32000.0
        print(f"[+] 收到音频: {len(audio_data)} 字节 ({audio_sec:.2f}s)", flush=True)
        server = self.gateway_server
        req_mode = qs.get("mode", [None])[0] or self.headers.get("X-Input-Mode")
        mode = req_mode.lower() if req_mode else server.state.mode
        if mode not in ("auto", "raw", "polish", "dialog"):
            mode = server.state.mode
        with temporary_wav_file(audio_data, apply_gain=True) as in_wav:
            heard, _, provider = server.asr_manager.transcribe(in_wav)
        if provider == "none":
            self._send_turn("", "语音识别服务暂时不可用，请稍后重试。", "", mode,
                            "error", "asr_unavailable", True, t_req)
            return
        if is_hallucination(heard, audio_sec):
            self._send_turn("...", "我没有听清，请按住按键再试一次。", "", mode,
                            "miss", "not_recognized", True, t_req)
            return
        self._process_turn(heard, mode, audio_sec, t_req)

    def _process_turn(self, heard, mode, audio_sec, t_req):
        server = self.gateway_server
        utterance, woke = strip_wake_word(heard)
        # 必须有冒号，普通的“打字速度/整理资料”等口述不能变成命令。
        explicit = re.match(r"^(原字|直接打|打字|帮我润色|润色|整理|改写)[：:]\s*(.+)$", utterance, re.S)
        typed = ""
        error = ""
        speak = False
        if explicit:
            command, content = explicit.groups()
            polish = command not in ("原字", "直接打", "打字")
            is_typing = True
        else:
            is_typing = mode != "dialog" and not woke
            content = utterance
            polish = mode == "polish" or (mode == "auto" and audio_sec >= server.polish_threshold)
        if is_typing:
            attempted = server.llm_manager.polish_text(content) if polish else content
            if polish:
                server.token_service.add_local_tokens(len(attempted) * 2)
            if CursorInjector.inject_text(attempted):
                typed = attempted
                reply = ("[已AI润色] " if polish else "[已原字键入] ") + typed
            else:
                reply = "[键入失败] 请检查 macOS 辅助功能权限"
                error = "injection_failed"
            speak = server.state.speak_typed
        else:
            reply = "我在，请说。" if not utterance else SystemActionExecutor.match_fast_command(
                utterance, server.token_service, server.weather_service
            )
            if not reply:
                reply, actions = server.llm_manager.chat(utterance)
                server.token_service.add_local_tokens(len(reply) * 2)
                if getattr(server.llm_manager, "last_chat_failed", False):
                    error = "llm_unavailable"
                for action in actions:
                    if action == "query_tokens":
                        reply = server.token_service.get_stats_text()
                    elif action == "query_weather":
                        reply = server.weather_service.get_weather_text()
                    else:
                        SystemActionExecutor.execute_action(action)
            speak = True
        self._send_turn(heard, reply, typed, mode, "error" if error else "ok", error, speak, t_req)

    def _send_turn(self, heard, reply, typed, mode, kind, error, speak, started):
        server = self.gateway_server
        pcm = server.tts.synthesize(reply) if speak else bytes(3200)
        meta = {"heard": heard, "reply": reply, "typed": typed, "mode": mode,
                "kind": kind, "error": error, "speak": speak}
        encoded = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        payload = struct.pack("!I", len(encoded)) + encoded + pcm
        server.state.record_turn(heard, reply, typed, mode, time.monotonic() - started, kind)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Heard", base64.b64encode(heard.encode("utf-8")).decode("ascii"))
        self.send_header("X-Reply", base64.b64encode(reply.encode("utf-8")).decode("ascii"))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)
        print(f"[✓] 回包完成: kind={kind}, {len(payload)} 字节", flush=True)
