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
import struct
import time
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from jarvis_gateway import __version__
from jarvis_gateway.actions.injector import CursorInjector
from jarvis_gateway.actions.system import SystemActionExecutor, strip_wake_word
from jarvis_gateway.asr.manager import is_hallucination
from jarvis_gateway.audio.converter import temporary_wav_file, wav_bytes_to_pcm16

if TYPE_CHECKING:
    from jarvis_gateway.server.server import UnifiedGatewayServer


class JarvisHTTPHandler(BaseHTTPRequestHandler):
    """Unified HTTP handler for all Jarvis Gateway requests."""

    # Reference to the container server
    gateway_server: UnifiedGatewayServer

    # Reject oversized uploads early (8 MB ≈ 4 minutes of 16 kHz/16-bit mono PCM).
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

        if provided and hmac.compare_digest(provided, token):
            return True

        self._reject(401, "Unauthorized: invalid or missing auth token")
        return False

    def _mutation_allowed(self) -> bool:
        """Guard state-changing requests: valid Host, no cross-site origin, valid auth."""
        if not self._host_allowed():
            self._reject(403, "Forbidden: unexpected Host header")
            return False
        if self._cross_site():
            self._reject(403, "Forbidden: cross-site request")
            return False
        return self._check_auth()

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # No wildcard CORS: /status includes recent dictation text and must not be
        # readable by arbitrary websites the user happens to visit.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
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
            if not self._check_auth():
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
                if not self._mutation_allowed():
                    return
                new_mode = qs["set"][0]
                if not state.set_mode(new_mode):
                    self._send_json({"error": "invalid mode", "options": ["auto", "raw", "polish", "dialog"]}, status=400)
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
            if not self._mutation_allowed():
                return
            new_mode = state.toggle_mode()
            self._send_json({"current_mode": new_mode, "mode": new_mode})
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
            import traceback

            traceback.print_exc()
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
        server = self.gateway_server
        state = server.state

        if not self._host_allowed():
            self._reject(403, "Forbidden: unexpected Host header")
            return
        if self._cross_site():
            self._reject(403, "Forbidden: cross-site request")
            return

        if path == "/mode":
            self._handle_mode_post(qs)
            return

        if path not in ("/chat", "/type"):
            self._send_json({"error": "endpoint not found"}, status=404)
            return

        # Optional auth verification
        if not self._check_auth():
            return

        t_req = time.time()
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            content_length = 0
        if content_length <= 0:
            self._reject(411, "Content-Length required")
            return
        if content_length > self.max_body_bytes:
            self._reject(413, f"audio too large (max {self.max_body_bytes} bytes)")
            return

        audio_data = self.rfile.read(content_length)
        if len(audio_data) < 100:
            self._reject(400, "audio too short")
            return
        if audio_data[:4] == b"\x1aE\xdf\xa3":
            self._reject(415, "WebM/Opus is not supported; send raw 16 kHz 16-bit mono PCM or a RIFF/WAV file")
            return
        if audio_data[:4] == b"RIFF":
            decoded = wav_bytes_to_pcm16(audio_data)
            if not decoded:
                self._reject(400, "unsupported WAV format; expected 16-bit PCM")
                return
            audio_data = decoded

        audio_sec = len(audio_data) / 32000.0
        print(f"\n[+] 收到音频数据: {len(audio_data)} 字节 (约 {audio_sec:.2f}秒)", flush=True)

        # Mode parsing
        req_mode = qs.get("mode", [None])[0] or self.headers.get("X-Input-Mode")
        active_mode = req_mode.lower() if req_mode else state.mode
        if active_mode not in ("auto", "raw", "polish", "dialog"):
            active_mode = state.mode

        # Save to temporary WAV with AGC applied
        with temporary_wav_file(audio_data, apply_gain=True) as in_wav:
            # 1. Speech Recognition
            heard_text, asr_elapsed, asr_tag = server.asr_manager.transcribe(in_wav)

            # Silence or hallucination detection
            if not heard_text or is_hallucination(heard_text, audio_sec):
                heard_text = "..."
                reply_text = "我没有听清，请按住按键再试一次。"
                pcm_out = server.tts.synthesize(reply_text)
                meta_json = json.dumps(
                    {"heard": heard_text, "reply": reply_text}, ensure_ascii=False
                ).encode("utf-8")
                payload = struct.pack("!I", len(meta_json)) + meta_json + pcm_out

                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                state.record_turn(heard_text, reply_text, "", active_mode, time.time() - t_req, "miss")
                return

            # 2. Intent Routing and Dispatch
            # Strip the wake word once; all routing below operates on the command text.
            utterance, woke = strip_wake_word(heard_text)
            reply_text = ""
            final_typed_text = ""
            should_play_speech = False
            is_type_action = False

            explicit_raw = ("原字：", "原字", "直接打：", "直接打", "打字：", "打字")
            explicit_polish = ("帮我润色：", "润色：", "帮我润色", "润色", "整理：", "整理", "改写：", "改写")

            if utterance.startswith(explicit_raw):
                # Explicit spoken prefix always wins: the user asked for literal typing.
                is_type_action = True
                clean = utterance
                for pfx in explicit_raw:
                    if clean.startswith(pfx):
                        clean = clean[len(pfx):].strip()
                        break
                final_typed_text = clean or utterance
                if CursorInjector.inject_text(final_typed_text):
                    reply_text = f"[已原字键入] {final_typed_text}"
                else:
                    reply_text = "[键入失败] 请检查 macOS 辅助功能权限"

            elif utterance.startswith(explicit_polish):
                is_type_action = True
                clean = utterance
                for pfx in explicit_polish:
                    if clean.startswith(pfx):
                        clean = clean[len(pfx):].strip()
                        break
                final_typed_text = server.llm_manager.polish_text(clean or utterance)
                server.token_service.add_local_tokens(len(final_typed_text) * 2)
                if CursorInjector.inject_text(final_typed_text):
                    reply_text = f"[已AI润色] {final_typed_text}"
                else:
                    reply_text = "[键入失败] 请检查 macOS 辅助功能权限"

            else:
                # Fast commands / conversation only make sense in dialog mode (or after the wake word).
                # In auto/raw/polish the user is dictating, so we must never hijack the sentence.
                dialog_intent = active_mode == "dialog" or woke
                fast_reply = None
                if dialog_intent and not utterance:
                    fast_reply = "我在，请说。"
                elif dialog_intent:
                    fast_reply = SystemActionExecutor.match_fast_command(
                        utterance, server.token_service, server.weather_service
                    )

                if fast_reply:
                    reply_text = fast_reply
                    should_play_speech = True
                    print(f"[*] 命中极速本地控制指令: '{reply_text}'", flush=True)

                elif dialog_intent:
                    reply_text, actions = server.llm_manager.chat(utterance)
                    server.token_service.add_local_tokens(len(reply_text) * 2)
                    for act in actions:
                        if act == "query_tokens":
                            reply_text = server.token_service.get_stats_text()
                        elif act == "query_weather":
                            reply_text = server.weather_service.get_weather_text()
                        else:
                            SystemActionExecutor.execute_action(act)
                    should_play_speech = True

                elif active_mode == "raw":
                    is_type_action = True
                    final_typed_text = utterance
                    if CursorInjector.inject_text(final_typed_text):
                        reply_text = f"[已原字键入] {final_typed_text}"
                    else:
                        reply_text = "[键入失败] 请检查 macOS 辅助功能权限"

                elif active_mode == "polish":
                    is_type_action = True
                    final_typed_text = server.llm_manager.polish_text(utterance)
                    server.token_service.add_local_tokens(len(final_typed_text) * 2)
                    if CursorInjector.inject_text(final_typed_text):
                        reply_text = f"[已AI润色] {final_typed_text}"
                    else:
                        reply_text = "[键入失败] 请检查 macOS 辅助功能权限"

                else:
                    # "auto" Mode: short speech -> raw, long speech -> polished
                    is_type_action = True
                    if audio_sec < server.polish_threshold:
                        print(
                            f"[*] 自动触发【极速原字】(时长 {audio_sec:.2f}s < {server.polish_threshold:g}s)",
                            flush=True,
                        )
                        final_typed_text = utterance
                        if CursorInjector.inject_text(final_typed_text):
                            reply_text = f"[已原字键入] {final_typed_text}"
                        else:
                            reply_text = "[键入失败] 请检查 macOS 辅助功能权限"
                    else:
                        print(
                            f"[*] 自动触发【AI智能润色】(时长 {audio_sec:.2f}s >= {server.polish_threshold:g}s)",
                            flush=True,
                        )
                        final_typed_text = server.llm_manager.polish_text(utterance)
                        server.token_service.add_local_tokens(len(final_typed_text) * 2)
                        if CursorInjector.inject_text(final_typed_text):
                            reply_text = f"[已AI润色] {final_typed_text}"
                        else:
                            reply_text = "[键入失败] 请检查 macOS 辅助功能权限"

            # 3. Output Speech Synthesis or 100ms Silence Frame
            if should_play_speech or (is_type_action and state.speak_typed):
                pcm_out = server.tts.synthesize(reply_text)
            else:
                # 100ms of silence (3200 bytes for 16kHz 16-bit mono)
                pcm_out = bytes(3200)

            # 4. Binary Wire Protocol: [4-byte big-endian JSON len] + [JSON bytes] + [PCM bytes]
            meta_json = json.dumps({
                "heard": heard_text,
                "reply": reply_text,
                "typed": final_typed_text,
                "mode": active_mode,
            }, ensure_ascii=False).encode("utf-8")

            payload = struct.pack("!I", len(meta_json)) + meta_json + pcm_out

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("X-Heard", base64.b64encode(heard_text.encode("utf-8")).decode("ascii"))
            self.send_header("X-Reply", base64.b64encode(reply_text.encode("utf-8")).decode("ascii"))
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)

            print(f"[✓] 回包发送完成: '{reply_text}' (载荷: {len(payload)} 字节)\n", flush=True)
            state.record_turn(heard_text, reply_text, final_typed_text, active_mode, time.time() - t_req, "ok")
