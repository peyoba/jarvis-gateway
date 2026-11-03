"""默认回归不访问云端、OpenToken 或真实桌面状态。"""

import pytest

from jarvis_gateway.services.token_service import TokenService
from jarvis_gateway.services.weather_service import WeatherService
from jarvis_gateway.state import GatewayState


@pytest.fixture(scope="session", autouse=True)
def isolated_test_environment(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        for name in ("JARVIS_AUTH_TOKEN", "JARVIS_CONFIG", "JARVIS_HTTP_LOG"):
            patch.delenv(name, raising=False)
        # 长期记忆默认落在 ~/.jarvis/memory；测试期间改到临时目录，绝不污染真实记忆。
        patch.setenv("JARVIS_MEMORY_DIR", str(tmp_path_factory.mktemp("jarvis-memory")))
        patch.setattr(TokenService, "_worker", lambda self: None)
        patch.setattr(WeatherService, "_worker", lambda self: None)
        patch.setattr(GatewayState, "get_lan_ip", lambda self: "127.0.0.1")
        yield
