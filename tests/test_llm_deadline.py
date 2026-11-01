"""真实 OpenAI SDK + 本机慢分块响应；不接触远程模型或真实凭据。"""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from jarvis_gateway.llm.client import LLMClientManager


@pytest.mark.parametrize("method", ["chat_result", "polish_text_result"])
def test_real_sdk_drip_response_is_cancelled_and_next_request_works(method):
    disconnected = threading.Event()
    requests = []
    clients = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(self.path)
            slow = len(requests) == 1
            body = json.dumps({
                "id": "test-local", "object": "chat.completion", "created": 0,
                "model": "test-model",
                "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": "正常回复文本",
                }, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            }, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            step = max(1, (len(body) + 14) // 15) if slow else len(body)
            try:
                for offset in range(0, len(body), step):
                    if slow and offset:
                        time.sleep(0.1)  # 不触发 1s 读取空闲 timeout，但总耗时约 1.4s，稳定越过总预算。
                    self.wfile.write(body[offset:offset + step])
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                disconnected.set()

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_port}/v1"

    def factory(**kwargs):
        client = AsyncOpenAI(**kwargs, http_client=DefaultAsyncHttpxClient(trust_env=False))
        clients.append(client)
        return client

    async def warmup():
        # 把一次性导入/连接池初始化开销与真正的 I/O 取消测量分开；不发送 HTTP。
        async with factory(api_key="synthetic-test-only", base_url=url, max_retries=0):
            pass

    cfg = {
        "total_timeout": 1.0,
        "primary": {
            "api_key": "synthetic-test-only", "base_url": url,
            "model": "test-model", "timeout": 1.0,
        },
        "fallback": {},
    }
    try:
        asyncio.run(warmup())
        manager = LLMClientManager(cfg, client_factory=factory)
        started = time.monotonic()
        failed = getattr(manager, method)("本机测试请求内容")
        elapsed = time.monotonic() - started
        assert getattr(failed, "error", getattr(failed, "reason", None)) == "llm_timeout"
        # 冷启动建连约 0.25s，1s 预算留足余量，同时仍验证总预算硬截止。
        assert elapsed < 1.3, f"1s 总预算被突破：{elapsed:.3f}s"
        assert failed.usage.calls == 1 and failed.usage.incomplete
        assert disconnected.wait(1), "取消后没有关闭真实 HTTP 连接"
        assert all(client.is_closed() for client in clients)
        # 同步接口再次 asyncio.run，必须创建新客户端而不是复用已关闭事件循环。
        good = getattr(manager, method)("第二次本机测试内容")
        assert getattr(good, "reply", getattr(good, "text", None)) == "正常回复文本"
        assert good.usage.calls == 1 and good.usage.total_tokens == 25
        assert not good.usage.estimated and not good.usage.incomplete
        assert len(requests) == 2 and len(clients) == 3
        assert all(client.is_closed() for client in clients)
        print(f"SDK deadline {method}: budget=0.200s elapsed={elapsed:.3f}s disconnected=true")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
