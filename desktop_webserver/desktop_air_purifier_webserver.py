
from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT.parent / "desktop_app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from desktop_air_purifier_app import (
    CONTROL_MODE_CLASSIC,
    CONTROL_MODE_LABELS,
    CONTROL_MODE_MANUAL,
    FILTER_STATE_FILE,
    PROFILE_CONFIG,
    SETTINGS_FILE,
    WEATHER_REFRESH_INTERVAL_SECONDS,
    AppConfig,
    ConfigManager,
    DataManager,
    FilterTracker,
    LOGGER,
    aqi_label,
    clamp,
    safe_int,
    sanitize_control_mode,
)

INDEX_PATH = ROOT / "desktop_web_dashboard.html"


def finite(value: Any, digits: int = 1) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def startup_urls(bind_host: str, bind_port: int) -> list[str]:
    urls: list[str] = []

    if bind_host in {"0.0.0.0", "::", ""}:
        urls.append(f"http://localhost:{bind_port}")
        urls.append(f"http://127.0.0.1:{bind_port}")
        try:
            _, _, addresses = socket.gethostbyname_ex(socket.gethostname())
            for address in sorted(set(addresses)):
                if address and not address.startswith("127."):
                    urls.append(f"http://{address}:{bind_port}")
        except Exception:
            pass
    elif bind_host in {"localhost", "127.0.0.1"}:
        urls.append(f"http://localhost:{bind_port}")
        urls.append(f"http://127.0.0.1:{bind_port}")
    else:
        urls.append(f"http://{bind_host}:{bind_port}")

    # Preserve order while removing duplicates.
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            deduped.append(url)
            seen.add(url)
    return deduped


class Backend:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.config_manager = ConfigManager(SETTINGS_FILE, LOGGER)
        self.data_manager = DataManager(LOGGER)
        self.config = self.config_manager.load()
        self.filter_tracker = FilterTracker(FILTER_STATE_FILE, self.config.filter_replacement_hours, LOGGER)
        self.last_esp: dict | None = None
        self.last_weather: dict | None = None
        self.last_air: dict | None = None
        self.last_weather_ts = 0.0

    def current_config(self) -> AppConfig:
        with self.lock:
            cfg = self.config_manager.load()
            if abs(cfg.filter_replacement_hours - self.config.filter_replacement_hours) > 0.001:
                self.filter_tracker.set_replacement_interval(cfg.filter_replacement_hours)
            self.config = cfg
            return cfg

    def update_config(self, payload: dict[str, Any]) -> AppConfig:
        base = self.current_config()
        cfg = self.config_manager.create_config(
            city=str(payload.get("city", base.city)),
            esp_base_url=str(payload.get("esp_base_url", base.esp_base_url)),
            openweather_api_key=str(payload.get("openweather_api_key", base.openweather_api_key)),
            ollama_url=str(payload.get("ollama_url", base.ollama_url)),
            ollama_model=str(payload.get("ollama_model", base.ollama_model)),
            control_mode=str(payload.get("control_mode", base.control_mode)),
            control_profile=str(payload.get("control_profile", base.control_profile)),
            filter_replacement_hours=payload.get("filter_replacement_hours", base.filter_replacement_hours),
            fallback=base,
            strict=True,
        )
        self.config_manager.save(cfg)
        self.filter_tracker.set_replacement_interval(cfg.filter_replacement_hours)
        with self.lock:
            self.config = cfg
        return cfg

    def _control(self, path: str) -> dict:
        cfg = self.current_config()
        result = self.data_manager.send_esp_command(cfg.esp_base_url, path)
        with self.lock:
            self.last_esp = dict(result)
        return dict(result)

    def set_mode(self, mode: str) -> dict:
        clean = sanitize_control_mode(mode, CONTROL_MODE_CLASSIC)
        result = self._control(f"/mode?value={clean}")
        self.update_config({"control_mode": clean})
        return result

    def set_profile(self, profile: str) -> dict:
        clean = (profile or "").strip().lower()
        if clean not in PROFILE_CONFIG:
            raise ValueError("Unknown profile")
        result = self._control(f"/profile?value={clean}")
        self.update_config({"control_profile": clean})
        return result

    def set_speed(self, speed: Any) -> dict:
        clean = int(clamp(safe_int(speed, 0), 0, 100))
        return self._control(f"/set?speed={clean}")

    def toggle(self) -> dict:
        return self._control("/toggle")

    def reset_filter(self) -> dict:
        state = self.filter_tracker.reset()
        return {
            "runtime_hours": round(state.runtime_hours, 1),
            "replacement_hours": round(state.replacement_interval_hours, 1),
        }
    def snapshot(self) -> dict[str, Any]:
        cfg = self.current_config()

        esp, esp_error = None, None
        try:
            esp = self.data_manager.read_esp_state(cfg.esp_base_url)
            with self.lock:
                self.last_esp = dict(esp)
        except Exception as error:
            esp_error = str(error)
            with self.lock:
                esp = dict(self.last_esp) if self.last_esp else None

        weather, air, weather_error, stale = None, None, None, False
        now = time.time()
        with self.lock:
            if self.last_weather and self.last_air and now - self.last_weather_ts < WEATHER_REFRESH_INTERVAL_SECONDS:
                weather, air = dict(self.last_weather), dict(self.last_air)

        if weather is None:
            try:
                weather, air = self.data_manager.read_openweather(cfg.city, cfg.openweather_api_key)
                with self.lock:
                    self.last_weather = dict(weather)
                    self.last_air = dict(air)
                    self.last_weather_ts = now
            except Exception as error:
                weather_error = str(error)
                stale = True
                with self.lock:
                    if self.last_weather and self.last_air:
                        weather, air = dict(self.last_weather), dict(self.last_air)

        esp_data = esp or {}
        speed = int(clamp(safe_int(esp_data.get("speed"), 0), 0, 100))
        filter_state = self.filter_tracker.update_runtime(speed) if esp is not None else self.filter_tracker.get_state()
        usage = self.filter_tracker.usage_percent(filter_state)

        weather_main = (weather or {}).get("main") or {}
        weather_desc = "--"
        weather_list = (weather or {}).get("weather") or []
        if weather_list and isinstance(weather_list[0], dict):
            weather_desc = str(weather_list[0].get("description", "--")).title()

        air_info = ((air or {}).get("list") or [{}])[0]
        air_main = air_info.get("main") or {}
        comps = air_info.get("components") or {}
        aqi = safe_int(air_main.get("aqi"), 0)

        mode = sanitize_control_mode(str(esp_data.get("control_mode") or cfg.control_mode), CONTROL_MODE_CLASSIC)
        profile = str(esp_data.get("control_profile") or cfg.control_profile or "aggressive").lower()
        if profile not in PROFILE_CONFIG:
            profile = "aggressive"

        temp = finite(esp_data.get("temp"), 1)
        hum = finite(esp_data.get("humidity"), 1)
        comfort = None
        if temp is not None and hum is not None:
            comfort = int(clamp(round(100.0 - abs(temp - 23.0) * 4.5 - abs(hum - 50.0) * 1.4), 0, 100))

        level = "healthy"
        summary = "All systems healthy"
        if esp_error and weather_error:
            level, summary = "critical", "ESP and weather unavailable"
        elif esp_error:
            level, summary = "degraded", "ESP unavailable"
        elif weather_error:
            level, summary = "degraded", "Weather unavailable"
        elif stale:
            level, summary = "degraded", "Using cached weather"

        return {
            "timestamp": int(time.time()),
            "health": {"level": level, "summary": summary, "esp_error": esp_error, "weather_error": weather_error},
            "config": {
                "city": cfg.city,
                "esp_base_url": cfg.esp_base_url,
                "openweather_api_key": cfg.openweather_api_key,
                "control_mode": cfg.control_mode,
                "control_profile": cfg.control_profile,
                "filter_replacement_hours": round(cfg.filter_replacement_hours, 1),
            },
            "control": {
                "mode": mode,
                "mode_label": CONTROL_MODE_LABELS.get(mode, "Unknown"),
                "profile": profile,
                "is_manual": mode == CONTROL_MODE_MANUAL,
            },
            "indoor": {
                "temp_c": temp,
                "humidity_pct": hum,
                "ds_temp_c": finite(esp_data.get("ds_temp"), 1),
                "rpm": safe_int(esp_data.get("rpm"), 0),
                "speed_pct": speed,
                "sht_ok": bool(esp_data.get("sht_ok")),
                "comfort_score": comfort,
            },
            "outdoor": {
                "temp_c": finite(weather_main.get("temp"), 1),
                "humidity_pct": finite(weather_main.get("humidity"), 0),
                "description": weather_desc,
            },
            "air": {
                "aqi": aqi if aqi > 0 else None,
                "aqi_label": aqi_label(aqi) if aqi > 0 else "Unknown",
                "pm2_5": finite(comps.get("pm2_5"), 1),
                "pm10": finite(comps.get("pm10"), 1),
                "no2": finite(comps.get("no2"), 1),
                "o3": finite(comps.get("o3"), 1),
            },
            "filter": {
                "runtime_hours": round(filter_state.runtime_hours, 1),
                "replacement_hours": round(filter_state.replacement_interval_hours, 1),
                "usage_percent": round(usage, 1),
                "left_percent": round(clamp(100.0 - usage, 0.0, 100.0), 1),
                "left_hours": round(max(0.0, filter_state.replacement_interval_hours - filter_state.runtime_hours), 1),
            },
            "esp": {
                "cmd_seq": safe_int(esp_data.get("cmd_seq"), 0),
                "last_cmd": str(esp_data.get("last_cmd", "--")),
                "cmd_age_ms": safe_int(esp_data.get("cmd_age_ms"), 0),
            },
        }

    def close(self) -> None:
        self.filter_tracker.flush()

class Handler(BaseHTTPRequestHandler):
    backend: Backend | None = None

    def log_message(self, format_text: str, *args: Any) -> None:
        LOGGER.debug("web: " + format_text, *args)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self) -> None:
        body = INDEX_PATH.read_bytes()
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self) -> dict[str, Any]:
        length = safe_int(self.headers.get("Content-Length", "0"), 0)
        if length <= 0:
            return {}
        data = self.rfile.read(length)
        if not data:
            return {}
        body = json.loads(data.decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("JSON body must be object")
        return body

    def _value(self, query: dict[str, list[str]], body: dict[str, Any]) -> str:
        if "value" in body:
            return str(body["value"])
        if "value" in query and query["value"]:
            return str(query["value"][0])
        return ""

    def do_GET(self) -> None:
        backend = self.backend
        if backend is None:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "Backend unavailable"})
            return

        route = urlparse(self.path).path
        if route == "/":
            self._send_html()
            return
        if route == "/api/state":
            self._send_json(HTTPStatus.OK, {"ok": True, "state": backend.snapshot()})
            return
        if route == "/api/config":
            cfg = backend.current_config()
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "config": {
                        "city": cfg.city,
                        "esp_base_url": cfg.esp_base_url,
                        "openweather_api_key": cfg.openweather_api_key,
                        "control_mode": cfg.control_mode,
                        "control_profile": cfg.control_profile,
                        "filter_replacement_hours": cfg.filter_replacement_hours,
                    },
                },
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        backend = self.backend
        if backend is None:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "Backend unavailable"})
            return

        parsed = urlparse(self.path)
        route, query = parsed.path, parse_qs(parsed.query)
        try:
            body = self._json_body()
            if route == "/api/control/mode":
                result = backend.set_mode(self._value(query, body))
            elif route == "/api/control/profile":
                result = backend.set_profile(self._value(query, body))
            elif route == "/api/control/speed":
                result = backend.set_speed(self._value(query, body))
            elif route == "/api/control/toggle":
                result = backend.toggle()
            elif route == "/api/filter/reset":
                result = backend.reset_filter()
            elif route == "/api/config":
                cfg = backend.update_config(body)
                result = {
                    "city": cfg.city,
                    "esp_base_url": cfg.esp_base_url,
                    "openweather_api_key": cfg.openweather_api_key,
                    "control_mode": cfg.control_mode,
                    "control_profile": cfg.control_profile,
                    "filter_replacement_hours": cfg.filter_replacement_hours,
                }
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
                return
            self._send_json(HTTPStatus.OK, {"ok": True, "result": result})
        except ValueError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
        except Exception as error:
            LOGGER.exception("POST route failed")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(error)})


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart Air Purifier desktop web dashboard")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    backend = Backend()
    Handler.backend = backend
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    LOGGER.info("Desktop dashboard server started: http://%s:%s", args.host, args.port)
    urls = startup_urls(args.host, args.port)
    print("", flush=True)
    print("Desktop web dashboard started.", flush=True)
    print(f"Bind address: {args.host}:{args.port}", flush=True)
    print("Open in your browser:", flush=True)
    for url in urls:
        print(f"  {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    print("", flush=True)

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOGGER.info("Desktop dashboard server stopped")
        print("\nDesktop web dashboard stopped.", flush=True)
    finally:
        backend.close()
        server.server_close()


if __name__ == "__main__":
    main()
