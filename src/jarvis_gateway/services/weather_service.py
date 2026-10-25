"""
Weather fetching service using wttr.in.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


class WeatherService:
    def __init__(self, city: str = "Beijing", city_label: str = "北京", poll_interval: float = 600.0):
        self.city = city
        self.city_label = city_label
        self.poll_interval = poll_interval
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._data: Dict[str, Any] = {
            "query_ok": False,
            "temperature": "--",
            "description": "--",
            "humidity": "--",
            "location": self.city_label,
        }

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="WeatherService")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.ident != threading.get_ident():
            self._thread.join(timeout=6)

    def fetch_weather(self):
        try:
            url = f"https://wttr.in/{urllib.parse.quote(self.city, safe='')}?format=j1"
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                cur = data["current_condition"][0]
                temp = cur.get("temp_C", "20")
                desc = cur["weatherDesc"][0]["value"] if cur.get("weatherDesc") else "Clear"
                hum = cur.get("humidity", "60") + "%"
                with self._lock:
                    self._data = {
                        "query_ok": True,
                        "temperature": str(temp),
                        "description": desc,
                        "humidity": hum,
                        "location": self.city_label,
                    }
        except Exception:
            with self._lock:
                self._data["query_ok"] = False

    def _worker(self):
        self.fetch_weather()
        while not self._stop_event.is_set():
            self._stop_event.wait(self.poll_interval)
            if not self._stop_event.is_set():
                self.fetch_weather()

    def get_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def get_weather_text(self) -> str:
        data = self.get_snapshot()
        loc = data.get("location", self.city_label)
        temp = data.get("temperature", "--")
        desc = data.get("description", "--")
        if not data.get("query_ok") or temp == "--":
            return "天气服务暂时不可用，请稍后再试。"
        return f"{loc}当前气温 {temp}度，天气状况 {desc}。"
