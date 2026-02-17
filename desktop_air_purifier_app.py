
import csv
import json
import logging
import math
import os
import re
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from tkinter import messagebox, ttk
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

try:
    from PIL import Image, ImageDraw, ImageTk
except Exception:
    Image = None
    ImageDraw = None
    ImageTk = None


SETTINGS_FILE = "desktop_app_settings.json"
LOG_FILE = "air_purifier_timeseries.csv"
DEBUG_LOG_FILE = "app_debug.log"
CALIBRATION_FILE = "fan_calibration.json"
FILTER_STATE_FILE = "filter_state.json"
FAN_ICON_FILE = "fan_icon.png"
FAN_ICON_SIZE_PX = 56

DEFAULT_OPENWEATHER_API_KEY = "56672a7fddd6d20e51a88155f0b4a0f2"
DEFAULT_ESP_BASE_URL = "http://192.168.1.132"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
DEFAULT_OLLAMA_MODEL = "llama3.1:8b"
DEFAULT_CITY = "San Jose"
DEFAULT_FILTER_REPLACEMENT_HOURS = 720.0

FAN_APP_MAX_RPM = 2200

MAX_CITY_LEN = 64
MAX_MODEL_LEN = 80
MAX_URL_LEN = 200
MAX_API_KEY_LEN = 64
MIN_FILTER_HOURS = 100.0
MAX_FILTER_HOURS = 5000.0
ESP_REFRESH_INTERVAL_MS = 8000
WEATHER_REFRESH_INTERVAL_SECONDS = 240
LLM_MIN_INTERVAL_SECONDS = 120
LLM_MAX_INTERVAL_SECONDS = 300
GRAPH_HISTORY_POINTS = 90

CITY_ALLOWED_RE = re.compile(r"[^A-Za-z0-9\s,.'-]+")
MODEL_ALLOWED_RE = re.compile(r"[^A-Za-z0-9._:-]+")
API_KEY_ALLOWED_RE = re.compile(r"[^A-Za-z0-9]+")

PROFILE_CONFIG = {
    "quiet": {
        "min_speed": 28,
        "max_speed": 82,
        "aqi_weight": 0.40,
        "pm25_weight": 0.30,
        "pm10_weight": 0.16,
        "shape": 1.15,
        "step": 7,
    },
    "balanced": {
        "min_speed": 34,
        "max_speed": 92,
        "aqi_weight": 0.48,
        "pm25_weight": 0.32,
        "pm10_weight": 0.10,
        "shape": 0.9,
        "step": 10,
    },
    "aggressive": {
        "min_speed": 38,
        "max_speed": 100,
        "aqi_weight": 0.50,
        "pm25_weight": 0.32,
        "pm10_weight": 0.10,
        "shape": 0.75,
        "step": 12,
    },
}

GRAPH_METRICS = [
    {"key": "aqi", "label": "AQI", "color": "#f59e0b", "unit": "", "precision": 0},
    {"key": "pm2_5", "label": "PM2.5", "color": "#f97316", "unit": " ug/m3", "precision": 1},
    {"key": "pm10", "label": "PM10", "color": "#fb7185", "unit": " ug/m3", "precision": 1},
    {"key": "room_temp", "label": "Room Temp", "color": "#f43f5e", "unit": " C", "precision": 1},
    {"key": "outside_temp", "label": "Outside Temp", "color": "#eab308", "unit": " C", "precision": 1},
    {"key": "room_humidity", "label": "Room Humidity", "color": "#14b8a6", "unit": "%", "precision": 0},
    {"key": "outside_humidity", "label": "Outside Humidity", "color": "#22d3ee", "unit": "%", "precision": 0},
]

GRAPH_METRICS_BY_KEY = {spec["key"]: spec for spec in GRAPH_METRICS}

GRAPH_GROUPS = [
    {"key": "temperature", "title": "Temperatures", "metrics": ["room_temp", "outside_temp"]},
    {"key": "humidity", "title": "Humidity", "metrics": ["room_humidity", "outside_humidity"]},
]

GRAPH_GROUPS_BY_KEY = {group["key"]: group for group in GRAPH_GROUPS}

if Image is not None:
    PIL_BICUBIC = Image.Resampling.BICUBIC if hasattr(Image, "Resampling") else Image.BICUBIC
else:
    PIL_BICUBIC = None


def configure_debug_logger() -> logging.Logger:
    logger = logging.getLogger("smart_air_purifier_desktop")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(DEBUG_LOG_FILE, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    logger.info("Debug logger initialized")
    return logger


LOGGER = configure_debug_logger()


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def aqi_label(aqi: int) -> str:
    return {
        1: "Good",
        2: "Fair",
        3: "Moderate",
        4: "Poor",
        5: "Very Poor",
    }.get(aqi, "Unknown")


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def sanitize_city(city: str) -> str:
    clean = CITY_ALLOWED_RE.sub("", (city or "").strip())
    clean = re.sub(r"\s+", " ", clean)
    clean = clean[:MAX_CITY_LEN].strip()
    return clean or DEFAULT_CITY


def sanitize_model_name(model_name: str) -> str:
    clean = MODEL_ALLOWED_RE.sub("", (model_name or "").strip())
    clean = clean[:MAX_MODEL_LEN].strip()
    return clean or DEFAULT_OLLAMA_MODEL


def sanitize_api_key(api_key: str) -> str:
    clean = API_KEY_ALLOWED_RE.sub("", (api_key or "").strip())
    return clean[:MAX_API_KEY_LEN]


def normalize_base_url(raw_url: str, default_url: str) -> str:
    candidate = (raw_url or "").strip() or default_url
    if len(candidate) > MAX_URL_LEN:
        raise ValueError("ESP URL is too long.")

    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("ESP URL must include http:// or https:// and a host.")
    if parsed.username or parsed.password:
        raise ValueError("ESP URL must not include credentials.")

    normalized = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    return normalized.rstrip("/")


def normalize_service_url(raw_url: str, default_url: str, require_path: bool = True) -> str:
    candidate = (raw_url or "").strip() or default_url
    if len(candidate) > MAX_URL_LEN:
        raise ValueError("Service URL is too long.")

    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Service URL must include http:// or https:// and a host.")
    if parsed.username or parsed.password:
        raise ValueError("Service URL must not include credentials.")

    path = parsed.path or ""
    if require_path and not path:
        raise ValueError("Service URL must include an API path.")

    normalized = urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
    return normalized.rstrip("/")


def redact_url_for_logs(url: str) -> str:
    try:
        parsed = urlparse(url)
        redacted_query = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() in {"appid", "api_key", "apikey", "token"}:
                redacted_query.append((key, "***"))
            else:
                redacted_query.append((key, value))
        query_string = urlencode(redacted_query, doseq=True)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query_string, ""))
    except Exception:
        return url


@dataclass(slots=True)
class AppConfig:
    city: str = DEFAULT_CITY
    esp_base_url: str = DEFAULT_ESP_BASE_URL
    openweather_api_key: str = DEFAULT_OPENWEATHER_API_KEY
    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_model: str = DEFAULT_OLLAMA_MODEL
    control_profile: str = "aggressive"
    filter_replacement_hours: float = DEFAULT_FILTER_REPLACEMENT_HOURS


@dataclass(slots=True)
class FilterState:
    runtime_hours: float = 0.0
    last_update_ts: float = 0.0
    replacement_interval_hours: float = DEFAULT_FILTER_REPLACEMENT_HOURS


@dataclass(slots=True)
class HealthStatus:
    label: str
    background: str
    foreground: str
    summary: str
    healthy: bool

class ConfigManager:
    def __init__(self, settings_file: str, logger: logging.Logger):
        self.settings_file = settings_file
        self.logger = logger

    def create_config(
        self,
        city: str,
        esp_base_url: str,
        openweather_api_key: str,
        ollama_url: str,
        ollama_model: str,
        control_profile: str,
        filter_replacement_hours: object,
        fallback: AppConfig | None = None,
        strict: bool = True,
    ) -> AppConfig:
        base = fallback or AppConfig()

        clean_city = sanitize_city(city)
        clean_profile = (control_profile or "").strip().lower()
        if clean_profile not in PROFILE_CONFIG:
            clean_profile = base.control_profile if base.control_profile in PROFILE_CONFIG else "aggressive"

        clean_api_key = sanitize_api_key(openweather_api_key)
        clean_model = sanitize_model_name(ollama_model)

        replacement_hours = safe_float(filter_replacement_hours, base.filter_replacement_hours)
        replacement_hours = float(clamp(replacement_hours, MIN_FILTER_HOURS, MAX_FILTER_HOURS))

        if strict:
            clean_esp_url = normalize_base_url(esp_base_url, base.esp_base_url)
            clean_ollama_url = normalize_service_url(ollama_url, base.ollama_url, require_path=False)
        else:
            try:
                clean_esp_url = normalize_base_url(esp_base_url, base.esp_base_url)
            except ValueError:
                clean_esp_url = base.esp_base_url
            try:
                clean_ollama_url = normalize_service_url(ollama_url, base.ollama_url, require_path=False)
            except ValueError:
                clean_ollama_url = base.ollama_url

        return AppConfig(
            city=clean_city,
            esp_base_url=clean_esp_url,
            openweather_api_key=clean_api_key,
            ollama_url=clean_ollama_url,
            ollama_model=clean_model,
            control_profile=clean_profile,
            filter_replacement_hours=replacement_hours,
        )

    def load(self) -> AppConfig:
        defaults = AppConfig()
        if not os.path.exists(self.settings_file):
            self.logger.info("Settings file not found; using defaults")
            return defaults

        try:
            with open(self.settings_file, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raise ValueError("Settings JSON must be an object")

            config = self.create_config(
                city=str(raw.get("city", defaults.city)),
                esp_base_url=str(raw.get("esp_base_url", defaults.esp_base_url)),
                openweather_api_key=str(raw.get("openweather_api_key", defaults.openweather_api_key)),
                ollama_url=str(raw.get("ollama_url", defaults.ollama_url)),
                ollama_model=str(raw.get("ollama_model", defaults.ollama_model)),
                control_profile=str(raw.get("control_profile", defaults.control_profile)),
                filter_replacement_hours=raw.get("filter_replacement_hours", defaults.filter_replacement_hours),
                fallback=defaults,
                strict=False,
            )
            self.logger.info("Settings loaded successfully")
            return config
        except Exception:
            self.logger.exception("Failed to load settings; using defaults")
            return defaults

    def save(self, config: AppConfig) -> None:
        validated = self.create_config(
            city=config.city,
            esp_base_url=config.esp_base_url,
            openweather_api_key=config.openweather_api_key,
            ollama_url=config.ollama_url,
            ollama_model=config.ollama_model,
            control_profile=config.control_profile,
            filter_replacement_hours=config.filter_replacement_hours,
            fallback=config,
            strict=True,
        )

        payload = {
            "city": validated.city,
            "esp_base_url": validated.esp_base_url,
            "openweather_api_key": validated.openweather_api_key,
            "ollama_url": validated.ollama_url,
            "ollama_model": validated.ollama_model,
            "control_profile": validated.control_profile,
            "filter_replacement_hours": round(validated.filter_replacement_hours, 2),
        }

        with open(self.settings_file, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        self.logger.info("Settings saved to %s", self.settings_file)


class DataLogger:
    FIELDNAMES = [
        "timestamp",
        "profile",
        "fail_safe",
        "aqi",
        "pm2_5",
        "pm10",
        "fan_speed_reported",
        "fan_speed_ai_applied",
        "fan_speed_ai_target",
        "room_temp_c",
        "room_humidity_pct",
        "outside_temp_c",
        "outside_humidity_pct",
        "cmd_seq",
        "last_cmd_ms",
    ]

    def __init__(self, csv_file: str, logger: logging.Logger):
        self.csv_file = csv_file
        self.logger = logger
        self.lock = threading.Lock()

    def log_csv_row(
        self,
        profile_name: str,
        fail_safe: bool,
        esp: dict,
        weather: dict,
        air: dict,
        fan_ai_speed: int | None,
        fan_ai_target: int | None,
    ) -> None:
        try:
            air_info = (air.get("list") or [{}])[0]
            main = air_info.get("main") or {}
            components = air_info.get("components") or {}
            weather_main = weather.get("main") or {}

            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "profile": profile_name,
                "fail_safe": int(bool(fail_safe)),
                "aqi": safe_int(main.get("aqi"), 0),
                "pm2_5": round(float(components.get("pm2_5", 0.0)), 2),
                "pm10": round(float(components.get("pm10", 0.0)), 2),
                "fan_speed_reported": esp.get("speed"),
                "fan_speed_ai_applied": fan_ai_speed if fan_ai_speed is not None else "",
                "fan_speed_ai_target": fan_ai_target if fan_ai_target is not None else "",
                "room_temp_c": esp.get("temp"),
                "room_humidity_pct": esp.get("humidity"),
                "outside_temp_c": weather_main.get("temp"),
                "outside_humidity_pct": weather_main.get("humidity"),
                "cmd_seq": esp.get("cmd_seq", ""),
                "last_cmd_ms": esp.get("last_cmd_ms", ""),
            }

            with self.lock:
                exists = os.path.exists(self.csv_file)
                with open(self.csv_file, "a", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=self.FIELDNAMES)
                    if not exists:
                        writer.writeheader()
                    writer.writerow(row)
        except Exception:
            self.logger.exception("Failed to append telemetry row")


class HealthMonitor:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.lock = threading.Lock()
        self.esp_failures = 0
        self.api_failures = 0
        self.ai_failures = 0
        self.last_esp_ok = True
        self.last_api_ok = True
        self.last_ai_ok = True
        self.last_error = ""

    def record_esp_success(self) -> None:
        with self.lock:
            self.last_esp_ok = True

    def record_esp_failure(self, reason: str) -> None:
        with self.lock:
            self.last_esp_ok = False
            self.esp_failures += 1
            self.last_error = reason
        self.logger.warning("ESP failure: %s", reason)

    def record_api_success(self) -> None:
        with self.lock:
            self.last_api_ok = True

    def record_api_failure(self, reason: str) -> None:
        with self.lock:
            self.last_api_ok = False
            self.api_failures += 1
            self.last_error = reason
        self.logger.warning("API failure: %s", reason)

    def record_ai_success(self) -> None:
        with self.lock:
            self.last_ai_ok = True

    def record_ai_failure(self, reason: str) -> None:
        with self.lock:
            self.last_ai_ok = False
            self.ai_failures += 1
            self.last_error = reason
        self.logger.warning("AI failure: %s", reason)

    def status(self) -> HealthStatus:
        with self.lock:
            counters = f"ESP:{self.esp_failures} API:{self.api_failures} AI:{self.ai_failures}"

            if not self.last_esp_ok:
                return HealthStatus(
                    label="Health: ESP Offline",
                    background="#7f1d1d",
                    foreground="#fecaca",
                    summary=f"ESP not reachable. {counters}",
                    healthy=False,
                )

            if not self.last_api_ok:
                return HealthStatus(
                    label="Health: API Degraded",
                    background="#78350f",
                    foreground="#fde68a",
                    summary=f"Weather/API unstable. {counters}",
                    healthy=False,
                )

            if not self.last_ai_ok:
                return HealthStatus(
                    label="Health: AI Degraded",
                    background="#7c2d12",
                    foreground="#ffedd5",
                    summary=f"LLM unavailable; fallback active. {counters}",
                    healthy=False,
                )

            if self.esp_failures + self.api_failures + self.ai_failures > 0:
                return HealthStatus(
                    label="Health: Recovering",
                    background="#365314",
                    foreground="#ecfccb",
                    summary=f"Recovered from earlier errors. {counters}",
                    healthy=True,
                )

            return HealthStatus(
                label="Health: Healthy",
                background="#14532d",
                foreground="#dcfce7",
                summary="All systems operating normally.",
                healthy=True,
            )

class CalibrationManager:
    def __init__(self, calibration_file: str, logger: logging.Logger):
        self.calibration_file = calibration_file
        self.logger = logger
        self.lock = threading.Lock()
        self._calibration = self._load_calibration()

    def _load_calibration(self) -> dict | None:
        if not os.path.exists(self.calibration_file):
            return None

        try:
            with open(self.calibration_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                return None

            samples = data.get("samples")
            if not isinstance(samples, list):
                return None

            clean_samples = []
            for sample in samples:
                if not isinstance(sample, dict):
                    continue
                pwm = safe_int(sample.get("pwm"), 0)
                rpm = safe_int(sample.get("rpm"), 0)
                pwm = int(clamp(pwm, 0, 100))
                rpm = int(clamp(rpm, 0, FAN_APP_MAX_RPM))
                clean_samples.append({"pwm": pwm, "rpm": rpm})

            if not clean_samples:
                return None

            clean_samples.sort(key=lambda item: item["pwm"])
            monotonic_samples = []
            max_seen = 0
            for sample in clean_samples:
                max_seen = max(max_seen, sample["rpm"])
                monotonic_samples.append({"pwm": sample["pwm"], "rpm": max_seen})

            spin_up_sample = next((item for item in monotonic_samples if item["rpm"] >= 250), monotonic_samples[0])
            max_sample = max(monotonic_samples, key=lambda item: item["rpm"])

            calibration = {
                "timestamp": str(data.get("timestamp", datetime.now(timezone.utc).isoformat())),
                "samples": monotonic_samples,
                "spin_up_pwm": safe_int(data.get("spin_up_pwm"), spin_up_sample["pwm"]),
                "spin_up_rpm": safe_int(data.get("spin_up_rpm"), spin_up_sample["rpm"]),
                "max_rpm": safe_int(data.get("max_rpm"), max_sample["rpm"]),
            }

            calibration["spin_up_pwm"] = int(clamp(calibration["spin_up_pwm"], 0, 100))
            calibration["spin_up_rpm"] = int(clamp(calibration["spin_up_rpm"], 0, FAN_APP_MAX_RPM))
            calibration["max_rpm"] = int(clamp(calibration["max_rpm"], calibration["spin_up_rpm"], FAN_APP_MAX_RPM))

            self.logger.info("Calibration loaded with %s samples", len(monotonic_samples))
            return calibration
        except Exception:
            self.logger.exception("Failed to load calibration")
            return None

    def get_calibration(self) -> dict | None:
        with self.lock:
            if not self._calibration:
                return None
            copy = dict(self._calibration)
            copy["samples"] = [dict(sample) for sample in self._calibration.get("samples", [])]
            return copy

    def save_calibration(self, calibration: dict) -> None:
        with self.lock:
            self._calibration = calibration
            with open(self.calibration_file, "w", encoding="utf-8") as handle:
                json.dump(calibration, handle, indent=2)
        self.logger.info("Calibration saved")

    def pwm_for_demand(self, demand_0_to_1: float, profile: dict) -> int | None:
        calibration = self.get_calibration()
        if not calibration:
            return None

        samples = calibration.get("samples")
        if not isinstance(samples, list) or len(samples) < 2:
            return None

        valid = []
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            pwm = safe_int(sample.get("pwm"), -1)
            rpm = safe_int(sample.get("rpm"), -1)
            if 0 <= pwm <= 100 and 0 <= rpm <= FAN_APP_MAX_RPM:
                valid.append({"pwm": pwm, "rpm": rpm})
        if len(valid) < 2:
            return None

        valid.sort(key=lambda item: item["rpm"])

        spin_up_rpm = safe_int(calibration.get("spin_up_rpm"), valid[0]["rpm"])
        max_rpm = safe_int(calibration.get("max_rpm"), valid[-1]["rpm"])
        if max_rpm <= spin_up_rpm:
            return None

        demand = clamp(demand_0_to_1, 0.0, 1.0)
        target_rpm = int(round(spin_up_rpm + demand * (max_rpm - spin_up_rpm)))

        if target_rpm <= valid[0]["rpm"]:
            return int(clamp(valid[0]["pwm"], profile["min_speed"], profile["max_speed"]))
        if target_rpm >= valid[-1]["rpm"]:
            return int(clamp(valid[-1]["pwm"], profile["min_speed"], profile["max_speed"]))

        for index in range(1, len(valid)):
            low = valid[index - 1]
            high = valid[index]
            if low["rpm"] <= target_rpm <= high["rpm"]:
                if high["rpm"] == low["rpm"]:
                    return int(clamp(high["pwm"], profile["min_speed"], profile["max_speed"]))
                fraction = (target_rpm - low["rpm"]) / float(high["rpm"] - low["rpm"])
                pwm = int(round(low["pwm"] + fraction * (high["pwm"] - low["pwm"])))
                return int(clamp(pwm, profile["min_speed"], profile["max_speed"]))

        return None


class FilterTracker:
    def __init__(self, state_file: str, replacement_interval_hours: float, logger: logging.Logger):
        self.state_file = state_file
        self.logger = logger
        self.lock = threading.Lock()
        self.default_interval = float(clamp(replacement_interval_hours, MIN_FILTER_HOURS, MAX_FILTER_HOURS))
        self._state = self._load_state()
        self._last_persist_ts = time.time()

    def _load_state(self) -> FilterState:
        now = time.time()
        if not os.path.exists(self.state_file):
            return FilterState(runtime_hours=0.0, last_update_ts=now, replacement_interval_hours=self.default_interval)

        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("Filter state must be an object")

            runtime = float(clamp(safe_float(data.get("runtime_hours"), 0.0), 0.0, 50000.0))
            last_update = safe_float(data.get("last_update_ts"), now)
            interval = float(
                clamp(
                    safe_float(data.get("replacement_interval_hours"), self.default_interval),
                    MIN_FILTER_HOURS,
                    MAX_FILTER_HOURS,
                )
            )
            return FilterState(runtime_hours=runtime, last_update_ts=last_update, replacement_interval_hours=interval)
        except Exception:
            self.logger.exception("Failed to load filter state; starting fresh")
            return FilterState(runtime_hours=0.0, last_update_ts=now, replacement_interval_hours=self.default_interval)

    def _persist_locked(self) -> None:
        try:
            with open(self.state_file, "w", encoding="utf-8") as handle:
                json.dump(asdict(self._state), handle, indent=2)
            self._last_persist_ts = time.time()
        except Exception:
            self.logger.exception("Failed to persist filter state")

    def flush(self) -> None:
        with self.lock:
            self._persist_locked()

    def set_replacement_interval(self, hours: float) -> None:
        with self.lock:
            self._state.replacement_interval_hours = float(clamp(hours, MIN_FILTER_HOURS, MAX_FILTER_HOURS))
            self._persist_locked()

    def update_runtime(self, speed_percent: int) -> FilterState:
        with self.lock:
            now = time.time()
            if self._state.last_update_ts <= 0:
                self._state.last_update_ts = now

            elapsed_seconds = max(0.0, now - self._state.last_update_ts)
            self._state.last_update_ts = now

            speed = clamp(float(speed_percent), 0.0, 100.0)
            wear_multiplier = 0.25 + (0.75 * (speed / 100.0))
            self._state.runtime_hours += (elapsed_seconds / 3600.0) * wear_multiplier

            if now - self._last_persist_ts >= 20:
                self._persist_locked()

            return FilterState(
                runtime_hours=self._state.runtime_hours,
                last_update_ts=self._state.last_update_ts,
                replacement_interval_hours=self._state.replacement_interval_hours,
            )

    def reset(self) -> FilterState:
        with self.lock:
            now = time.time()
            self._state.runtime_hours = 0.0
            self._state.last_update_ts = now
            self._persist_locked()
            return FilterState(
                runtime_hours=self._state.runtime_hours,
                last_update_ts=self._state.last_update_ts,
                replacement_interval_hours=self._state.replacement_interval_hours,
            )

    def get_state(self) -> FilterState:
        with self.lock:
            return FilterState(
                runtime_hours=self._state.runtime_hours,
                last_update_ts=self._state.last_update_ts,
                replacement_interval_hours=self._state.replacement_interval_hours,
            )

    def usage_percent(self, state: FilterState | None = None) -> float:
        snapshot = state or self.get_state()
        if snapshot.replacement_interval_hours <= 0:
            return 0.0
        return max(0.0, (snapshot.runtime_hours / snapshot.replacement_interval_hours) * 100.0)

class DataManager:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.geo_cache_lock = threading.Lock()
        self.geo_cache: dict[str, tuple[float, float]] = {}

    def _request(
        self,
        url: str,
        timeout: int,
        method: str = "GET",
        payload: bytes | None = None,
        headers: dict | None = None,
        expect_json: bool = True,
        max_attempts: int = 3,
    ):
        safe_url = redact_url_for_logs(url)
        req_headers = headers or {}
        last_error: Exception | None = None

        for attempt in range(max_attempts):
            try:
                request = Request(url, data=payload, headers=req_headers, method=method)
                with urlopen(request, timeout=timeout) as response:
                    response_text = response.read().decode("utf-8")

                if expect_json:
                    return json.loads(response_text)
                return response_text
            except HTTPError as error:
                body = ""
                try:
                    body = error.read().decode("utf-8")
                except Exception:
                    body = ""
                message = body or str(error.reason)
                last_error = RuntimeError(f"HTTP {error.code}: {message}")
            except URLError as error:
                last_error = RuntimeError(f"Network error: {error.reason}")
            except TimeoutError:
                last_error = RuntimeError("Request timed out")
            except json.JSONDecodeError as error:
                last_error = RuntimeError(f"Invalid JSON response: {error}")
            except Exception as error:
                last_error = RuntimeError(str(error))

            if attempt < max_attempts - 1:
                delay_seconds = 2 ** attempt
                self.logger.warning(
                    "Request failed (%s). Retrying in %ss [%s]",
                    last_error,
                    delay_seconds,
                    safe_url,
                )
                time.sleep(delay_seconds)
            else:
                self.logger.error("Request failed after retries [%s]: %s", safe_url, last_error)

        if last_error:
            raise last_error
        raise RuntimeError("Request failed without an error message")

    def request_json(
        self,
        url: str,
        timeout: int = 8,
        method: str = "GET",
        payload: bytes | None = None,
        headers: dict | None = None,
        max_attempts: int = 3,
    ) -> dict | list:
        return self._request(
            url=url,
            timeout=timeout,
            method=method,
            payload=payload,
            headers=headers,
            expect_json=True,
            max_attempts=max_attempts,
        )

    def request_text(self, url: str, timeout: int = 8, max_attempts: int = 3) -> str:
        return str(
            self._request(
                url=url,
                timeout=timeout,
                method="GET",
                payload=None,
                headers=None,
                expect_json=False,
                max_attempts=max_attempts,
            )
        )

    def read_esp_state(self, esp_base_url: str) -> dict:
        try:
            return dict(self.request_json(f"{esp_base_url}/state", timeout=6))
        except Exception as state_error:
            self.logger.warning("ESP /state failed, falling back to /data: %s", state_error)
            return dict(self.request_json(f"{esp_base_url}/data", timeout=6))

    def send_esp_command(self, esp_base_url: str, path: str) -> dict:
        endpoint = f"{esp_base_url}{path}"
        try:
            return dict(self.request_json(endpoint, timeout=6))
        except Exception as json_error:
            self.logger.warning("ESP JSON command fallback (%s): %s", redact_url_for_logs(endpoint), json_error)
            self.request_text(endpoint, timeout=6)
            return self.read_esp_state(esp_base_url)

    def read_openweather(self, city: str, api_key: str) -> tuple[dict, dict]:
        if not api_key:
            raise RuntimeError("OpenWeather API key is empty")

        city_key = (city or "").strip().lower()
        lat = None
        lon = None
        with self.geo_cache_lock:
            cached = self.geo_cache.get(city_key)
        if cached is not None:
            lat, lon = cached
        else:
            city_q = quote_plus(city)
            geo = self.request_json(
                f"http://api.openweathermap.org/geo/1.0/direct?q={city_q}&limit=1&appid={api_key}",
                timeout=8,
            )
            if not geo:
                raise RuntimeError(f"City not found: {city}")
            lat = float(geo[0]["lat"])
            lon = float(geo[0]["lon"])
            with self.geo_cache_lock:
                self.geo_cache[city_key] = (lat, lon)

        weather = self.request_json(
            f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={api_key}&units=metric",
            timeout=8,
        )
        air = self.request_json(
            f"https://api.openweathermap.org/data/2.5/air_pollution?lat={lat}&lon={lon}&appid={api_key}",
            timeout=8,
        )
        return dict(weather), dict(air)

    def ollama_generate(self, prompt: str, model: str, ollama_url: str, timeout: int = 20) -> str:
        payload = json.dumps(
            {
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.2},
            }
        ).encode("utf-8")
        response = self.request_json(
            url=ollama_url,
            timeout=timeout,
            method="POST",
            payload=payload,
            headers={"Content-Type": "application/json"},
            max_attempts=3,
        )
        return str(response.get("response", "")).strip()

class AIController:
    def __init__(self, data_manager: DataManager, health_monitor: HealthMonitor, logger: logging.Logger):
        self.data_manager = data_manager
        self.health_monitor = health_monitor
        self.logger = logger

        self.last_fan_ai_ts = 0.0
        self.last_advice_ts = 0.0
        self.last_pollution_comment_ts = 0.0

        self.cached_fan_speed: int | None = None
        self.cached_advice: str | None = None
        self.cached_pollution_comment: str | None = None
        self.last_fan_context: dict | None = None
        self.last_weather_context: dict | None = None
        self.last_pollution_context: dict | None = None

    @staticmethod
    def extract_speed(text: str, min_speed: int, max_speed: int, default_speed: int) -> int:
        match = re.search(r"(\d{1,3})", text or "")
        if not match:
            return int(clamp(default_speed, min_speed, max_speed))
        value = safe_int(match.group(1), default_speed)
        return int(clamp(value, min_speed, max_speed))

    def curve_baseline_speed(self, aqi: int, components: dict, profile: dict, calibration_manager: CalibrationManager) -> int:
        pm25 = float(components.get("pm2_5", 0.0))
        pm10 = float(components.get("pm10", 0.0))
        no2 = float(components.get("no2", 0.0))
        o3 = float(components.get("o3", 0.0))

        risk = 0.0
        risk += (max(1, min(5, safe_int(aqi, 3))) - 1) / 4.0 * profile["aqi_weight"]
        risk += clamp(pm25 / 55.0, 0.0, 1.0) * profile["pm25_weight"]
        risk += clamp(pm10 / 120.0, 0.0, 1.0) * profile["pm10_weight"]
        risk += clamp(no2 / 200.0, 0.0, 1.0) * 0.05
        risk += clamp(o3 / 180.0, 0.0, 1.0) * 0.05
        risk = clamp(risk, 0.0, 1.0)

        shaped = math.pow(risk, profile["shape"])
        eased = 0.5 - (0.5 * math.cos(math.pi * shaped))

        calibrated_pwm = calibration_manager.pwm_for_demand(eased, profile)
        if calibrated_pwm is not None:
            return int(clamp(calibrated_pwm, profile["min_speed"], profile["max_speed"]))

        return int(round(profile["min_speed"] + (eased * (profile["max_speed"] - profile["min_speed"]))))

    def _should_query_llm(
        self,
        now: float,
        last_ts: float,
        last_context: dict | None,
        current_context: dict,
    ) -> bool:
        elapsed = now - last_ts
        if elapsed >= LLM_MAX_INTERVAL_SECONDS:
            return True
        if elapsed < LLM_MIN_INTERVAL_SECONDS:
            return False
        if last_context is None:
            return True
        return self._context_changed(last_context, current_context)

    @staticmethod
    def _context_changed(previous: dict, current: dict) -> bool:
        for key, current_value in current.items():
            previous_value = previous.get(key)
            if isinstance(current_value, (int, float)) and isinstance(previous_value, (int, float)):
                threshold = 0.5
                if key in {"aqi"}:
                    threshold = 1.0
                elif key in {"pm2_5", "pm10"}:
                    threshold = 6.0
                elif key in {"room_temp", "outside_temp"}:
                    threshold = 1.2
                elif key in {"room_humidity", "outside_humidity"}:
                    threshold = 8.0
                if abs(float(current_value) - float(previous_value)) >= threshold:
                    return True
            else:
                if str(previous_value) != str(current_value):
                    return True
        return False

    def decide_fan_target(
        self,
        config: AppConfig,
        profile_name: str,
        esp: dict,
        weather: dict,
        air: dict,
        baseline: int,
        force_fail_safe: bool = False,
    ) -> tuple[int, bool]:
        profile = PROFILE_CONFIG.get(profile_name, PROFILE_CONFIG["aggressive"])
        now = time.time()

        if force_fail_safe:
            self.cached_fan_speed = baseline
            return baseline, True

        ai_target = baseline
        llm_failed = False
        room_temp = safe_float(esp.get("temp"), 0.0)
        humidity = safe_float(esp.get("humidity"), 0.0)
        outside_temp = safe_float(weather.get("main", {}).get("temp"), 0.0)
        air_main = safe_int(air.get("list", [{}])[0].get("main", {}).get("aqi"), 3)
        components = air.get("list", [{}])[0].get("components", {})
        fan_context = {
            "aqi": air_main,
            "pm2_5": safe_float(components.get("pm2_5"), 0.0),
            "pm10": safe_float(components.get("pm10"), 0.0),
            "no2": safe_float(components.get("no2"), 0.0),
            "o3": safe_float(components.get("o3"), 0.0),
            "room_temp": room_temp,
            "outside_temp": outside_temp,
            "room_humidity": humidity,
        }

        should_query_llm = self.cached_fan_speed is None or self._should_query_llm(
            now=now,
            last_ts=self.last_fan_ai_ts,
            last_context=self.last_fan_context,
            current_context=fan_context,
        )
        if should_query_llm:

            prompt = (
                "You are controlling a DIY purifier with a strong 12V industrial fan and Xiaomi filter. "
                f"Current control profile is {profile_name}. "
                f"Return one integer only from {profile['min_speed']} to {profile['max_speed']}. "
                f"AQI={air_main} ({aqi_label(air_main)}), PM2.5={components.get('pm2_5', 0):.1f}, "
                f"PM10={components.get('pm10', 0):.1f}, NO2={components.get('no2', 0):.1f}, "
                f"O3={components.get('o3', 0):.1f}, RoomTemp={room_temp}, "
                f"RoomHumidity={humidity}, OutsideTemp={outside_temp}. "
                f"Baseline speed suggestion is {baseline}."
            )

            try:
                raw = self.data_manager.ollama_generate(
                    prompt=prompt,
                    model=config.ollama_model,
                    ollama_url=config.ollama_url,
                    timeout=20,
                )
                ai_target = self.extract_speed(raw, profile["min_speed"], profile["max_speed"], baseline)
                self.cached_fan_speed = ai_target
                self.health_monitor.record_ai_success()
                self.logger.info("AI fan target generated: %s", ai_target)
            except Exception as error:
                self.logger.warning("AI fan generation failed; using baseline: %s", error)
                self.health_monitor.record_ai_failure(str(error))
                ai_target = baseline
                self.cached_fan_speed = baseline
                llm_failed = True

            self.last_fan_ai_ts = now
            self.last_fan_context = fan_context
        else:
            ai_target = safe_int(self.cached_fan_speed, baseline)

        blend_weight = 0.35 if not llm_failed else 0.0
        blended_target = int(round((baseline * (1.0 - blend_weight)) + (ai_target * blend_weight)))
        blended_target = int(clamp(blended_target, profile["min_speed"], profile["max_speed"]))
        return blended_target, llm_failed

    def temperature_advice(self, config: AppConfig, esp: dict, weather: dict, force_fail_safe: bool = False) -> str:
        room_temp = safe_float(esp.get("temp"), 0.0)
        outside_temp = safe_float(weather.get("main", {}).get("temp"), 0.0)
        room_humidity = safe_float(esp.get("humidity"), 0.0)
        outside_humidity = safe_float(weather.get("main", {}).get("humidity"), 0.0)
        weather_desc = str(weather.get("weather", [{}])[0].get("description", "--")).lower()
        weather_context = {
            "room_temp": room_temp,
            "outside_temp": outside_temp,
            "room_humidity": room_humidity,
            "outside_humidity": outside_humidity,
            "weather_desc": weather_desc,
            "wind_speed": safe_float(weather.get("wind", {}).get("speed"), 0.0),
        }

        if force_fail_safe:
            advice = self._fallback_weather_comment(room_temp, outside_temp, outside_humidity, weather_desc)
            self.cached_advice = advice
            return advice

        now = time.time()
        should_query_llm = not self.cached_advice or self._should_query_llm(
            now=now,
            last_ts=self.last_advice_ts,
            last_context=self.last_weather_context,
            current_context=weather_context,
        )
        if not should_query_llm:
            return self.cached_advice or self._fallback_weather_comment(
                room_temp,
                outside_temp,
                outside_humidity,
                weather_desc,
            )

        prompt = (
            "Provide one short practical weather note for the next few hours. "
            "Focus on comfort, ventilation, rain/wind, and air freshness; do not focus on clothing. "
            f"Room temp {room_temp:.1f}C, room humidity {room_humidity:.0f}%, "
            f"outside temp {outside_temp:.1f}C, outside humidity {outside_humidity:.0f}%, "
            f"conditions {weather.get('weather', [{}])[0].get('description', '--')}, "
            f"wind {safe_float(weather.get('wind', {}).get('speed'), 0.0):.1f} m/s."
        )

        try:
            advice = self.data_manager.ollama_generate(
                prompt=prompt,
                model=config.ollama_model,
                ollama_url=config.ollama_url,
                timeout=20,
            )
            if not advice:
                raise RuntimeError("LLM returned empty advice")
            self.cached_advice = advice
            self.health_monitor.record_ai_success()
        except Exception as error:
            self.logger.warning("Weather comment fallback: %s", error)
            self.health_monitor.record_ai_failure(str(error))
            self.cached_advice = self._fallback_weather_comment(
                room_temp,
                outside_temp,
                outside_humidity,
                weather_desc,
            )

        self.last_advice_ts = now
        self.last_weather_context = weather_context
        return self.cached_advice

    def pollution_comment(self, config: AppConfig, air: dict, force_fail_safe: bool = False) -> str:
        air_info = (air.get("list") or [{}])[0]
        aqi = safe_int((air_info.get("main") or {}).get("aqi"), 3)
        components = air_info.get("components") or {}
        pollution_context = {
            "aqi": aqi,
            "pm2_5": safe_float(components.get("pm2_5"), 0.0),
            "pm10": safe_float(components.get("pm10"), 0.0),
            "no2": safe_float(components.get("no2"), 0.0),
            "o3": safe_float(components.get("o3"), 0.0),
        }

        if force_fail_safe:
            comment = self._fallback_pollution_comment(aqi, components)
            self.cached_pollution_comment = comment
            return comment

        now = time.time()
        should_query_llm = not self.cached_pollution_comment or self._should_query_llm(
            now=now,
            last_ts=self.last_pollution_comment_ts,
            last_context=self.last_pollution_context,
            current_context=pollution_context,
        )
        if not should_query_llm:
            return self.cached_pollution_comment or self._fallback_pollution_comment(aqi, components)

        prompt = (
            "In one short sentence, explain what this outdoor air quality means for comfort or health "
            "and whether to keep purifier fan low, medium, or high. "
            f"AQI={aqi} ({aqi_label(aqi)}), PM2.5={components.get('pm2_5', 0):.1f}, "
            f"PM10={components.get('pm10', 0):.1f}, NO2={components.get('no2', 0):.1f}, "
            f"O3={components.get('o3', 0):.1f}."
        )

        try:
            comment = self.data_manager.ollama_generate(
                prompt=prompt,
                model=config.ollama_model,
                ollama_url=config.ollama_url,
                timeout=20,
            )
            if not comment:
                raise RuntimeError("LLM returned empty pollution comment")
            self.cached_pollution_comment = comment
            self.health_monitor.record_ai_success()
        except Exception as error:
            self.logger.warning("Pollution comment fallback: %s", error)
            self.health_monitor.record_ai_failure(str(error))
            self.cached_pollution_comment = self._fallback_pollution_comment(aqi, components)

        self.last_pollution_comment_ts = now
        self.last_pollution_context = pollution_context
        return self.cached_pollution_comment

    @staticmethod
    def _fallback_weather_comment(
        room_temp: float,
        outside_temp: float,
        outside_humidity: float,
        weather_desc: str,
    ) -> str:
        if "rain" in weather_desc or "storm" in weather_desc or "drizzle" in weather_desc:
            return "Rain is likely outside; keep windows mostly closed and run steady purifier airflow."
        if "fog" in weather_desc or "mist" in weather_desc:
            return "Outdoor air is misty; short ventilation bursts are better than long open-window periods."
        if outside_humidity >= 78:
            return "Outside humidity is high right now; limit long ventilation and keep indoor airflow consistent."

        delta = room_temp - outside_temp
        if delta > 5:
            return "Outside is noticeably cooler than indoors; brief ventilation can help cool the room."
        if delta < -5:
            return "Outside is warmer than indoors; keep windows limited during peak heat hours."
        return "Weather is fairly stable; keep moderate airflow and ventilate briefly as needed."

    @staticmethod
    def _fallback_pollution_comment(aqi: int, components: dict) -> str:
        pm25 = float(components.get("pm2_5", 0.0))
        if aqi <= 2 and pm25 < 25:
            return "Air looks clean right now; low purifier speed is usually enough."
        if aqi == 3 or pm25 < 55:
            return "Air is moderate; medium fan speed helps keep indoor air fresher."
        if aqi == 4 or pm25 < 90:
            return "Air quality is poor; run medium-high to high fan speed and limit outside air intake."
        return "Air quality is very poor; keep purifier on high and reduce exposure to outdoor air."


class FanController:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.lock = threading.Lock()
        self.ai_target_speed: int | None = None
        self.ai_applied_speed: int | None = None
        self.last_ai_push_ts = 0.0
        self.last_manual_send_ts = 0.0

    def reset_ai_state(self) -> None:
        with self.lock:
            self.ai_target_speed = None
            self.ai_applied_speed = None
            self.last_ai_push_ts = 0.0

    def compute_applied_speed(self, target_speed: int, current_speed: int, profile: dict) -> tuple[int, int]:
        with self.lock:
            min_speed = profile["min_speed"]
            max_speed = profile["max_speed"]
            step_limit = int(profile["step"])

            self.ai_target_speed = int(clamp(target_speed, min_speed, max_speed))
            if self.ai_applied_speed is None:
                self.ai_applied_speed = int(clamp(current_speed, min_speed, max_speed))

            error = self.ai_target_speed - self.ai_applied_speed
            step = int(clamp(error, -step_limit, step_limit))
            if abs(error) >= 2:
                self.ai_applied_speed += step

            self.ai_applied_speed = int(clamp(self.ai_applied_speed, min_speed, max_speed))
            return self.ai_applied_speed, self.ai_target_speed

    def should_push(self, current_speed: int) -> bool:
        with self.lock:
            if self.ai_applied_speed is None:
                return False
            return abs(self.ai_applied_speed - current_speed) >= 2 and (time.time() - self.last_ai_push_ts) > 3.0

    def mark_push(self) -> None:
        with self.lock:
            self.last_ai_push_ts = time.time()

    def manual_send_allowed(self, min_interval_seconds: float = 0.2) -> bool:
        with self.lock:
            now = time.time()
            if now - self.last_manual_send_ts < min_interval_seconds:
                return False
            self.last_manual_send_ts = now
            return True

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.logger = LOGGER

        self.config_manager = ConfigManager(SETTINGS_FILE, self.logger)
        self.config = self.config_manager.load()

        self.health_monitor = HealthMonitor(self.logger)
        self.data_logger = DataLogger(LOG_FILE, self.logger)
        self.data_manager = DataManager(self.logger)
        self.calibration_manager = CalibrationManager(CALIBRATION_FILE, self.logger)
        self.filter_tracker = FilterTracker(FILTER_STATE_FILE, self.config.filter_replacement_hours, self.logger)
        self.ai_controller = AIController(self.data_manager, self.health_monitor, self.logger)
        self.fan_controller = FanController(self.logger)

        self.city_var = tk.StringVar(value=self.config.city)
        self.esp_url_var = tk.StringVar(value=self.config.esp_base_url)
        self.api_key_var = tk.StringVar(value=self.config.openweather_api_key)
        self.ollama_url_var = tk.StringVar(value=self.config.ollama_url)
        self.model_var = tk.StringVar(value=self.config.ollama_model)
        self.profile_var = tk.StringVar(value=self.config.control_profile)
        self.filter_hours_var = tk.StringVar(value=str(int(self.config.filter_replacement_hours)))

        self.ai_auto_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Ready")
        self.health_summary_var = tk.StringVar(value="Health counters: ESP:0 API:0 AI:0")

        self.shutdown_event = threading.Event()
        self.refresh_lock = threading.Lock()
        self.refresh_in_progress = False
        self.autotune_in_progress = False
        self.slider_syncing = False
        self.manual_state_lock = threading.Lock()
        self.manual_pending_speed: int | None = None
        self.manual_sender_running = False
        self.manual_override_until = 0.0

        self.last_esp: dict | None = None
        self.last_weather: dict | None = None
        self.last_air: dict | None = None
        self.last_weather_fetch_ts = 0.0
        self.fail_safe_mode = False

        self.esp_auto_mode = True
        self.current_speed_pct = 0

        self.fan_anim_frames = ["|", "/", "-", "\\"]
        self.fan_anim_index = 0
        self.fan_icon_base = None
        self.fan_icon_frame = None
        self.fan_icon_angle = 0.0
        self.graph_history = {spec["key"]: deque(maxlen=GRAPH_HISTORY_POINTS) for spec in GRAPH_METRICS}
        self.graph_canvases: dict[str, tk.Canvas] = {}
        self.graph_legend_labels: dict[str, tk.Label] = {}
        self.filter_life_canvas: tk.Canvas | None = None
        self.filter_left_label: ttk.Label | None = None
        self.filter_left_pct = 100.0
        self.filter_life_color = "#22c55e"

        self.root.title("Smart Air Purifier Desktop")
        self.root.geometry("1040x650")
        self.root.configure(bg="#0b132b")
        self._init_fan_icon()

        self._build_ui()
        self._bind_shortcuts()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._update_filter_labels(self.filter_tracker.get_state())
        self._update_calibration_label()
        self._update_health_indicator()
        self.root.after(250, self._draw_metric_graphs)

        self._schedule_refresh()
        self._tick_fan_animation()

    def _create_default_fan_icon(self, output_path: str) -> None:
        if Image is None or ImageDraw is None:
            return

        size = 300
        center = size // 2
        blade_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        blade_draw = ImageDraw.Draw(blade_layer)

        # Build a single rounded blade, then rotate it for 4-blade propeller layout.
        blade_draw.ellipse((center - 40, 18, center + 40, center + 120), fill=(0, 0, 0, 255))
        blade_draw.ellipse((center - 30, center - 6, center + 30, center + 62), fill=(0, 0, 0, 255))
        blade_draw.polygon(
            [
                (center - 34, center + 30),
                (center + 34, center + 30),
                (center + 20, center + 138),
                (center - 20, center + 138),
            ],
            fill=(0, 0, 0, 255),
        )

        icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        for angle in (0, 90, 180, 270):
            rotated_blade = blade_layer.rotate(angle, resample=PIL_BICUBIC, expand=False)
            icon.alpha_composite(rotated_blade)

        hub_draw = ImageDraw.Draw(icon)
        hub_draw.ellipse((center - 34, center - 34, center + 34, center + 34), fill=(0, 0, 0, 255))
        hub_draw.ellipse((center - 10, center - 10, center + 10, center + 10), fill=(0, 0, 0, 0))
        icon.save(output_path, format="PNG")

    def _init_fan_icon(self) -> None:
        if Image is None or ImageTk is None:
            self.logger.warning("Pillow is not installed. Falling back to text fan spinner.")
            return

        try:
            if not os.path.exists(FAN_ICON_FILE):
                self._create_default_fan_icon(FAN_ICON_FILE)

            image = Image.open(FAN_ICON_FILE).convert("RGBA")
            image = image.resize((FAN_ICON_SIZE_PX, FAN_ICON_SIZE_PX), resample=PIL_BICUBIC)
            self.fan_icon_base = image
            self.fan_icon_frame = ImageTk.PhotoImage(image)
            self.logger.info("Fan icon loaded from %s", FAN_ICON_FILE)
        except Exception:
            self.logger.exception("Failed to load fan icon image. Falling back to text spinner.")
            self.fan_icon_base = None
            self.fan_icon_frame = None

    def _build_ui(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Card.TFrame", background="#1c2541")
        style.configure("PrimaryCard.TFrame", background="#1c2541")
        style.configure("FooterCard.TFrame", background="#1c2541")
        style.configure("SubCard.TFrame", background="#1f2a52")
        style.configure("Title.TLabel", background="#0b132b", foreground="#f7f9fc", font=("Segoe UI", 18, "bold"))
        style.configure("CardTitle.TLabel", background="#1c2541", foreground="#b6c2d9", font=("Segoe UI", 10, "bold"))
        style.configure("SubCardTitle.TLabel", background="#1f2a52", foreground="#b6c2d9", font=("Segoe UI", 10, "bold"))
        style.configure("Big.TLabel", background="#1c2541", foreground="#f7f9fc", font=("Segoe UI", 20, "bold"))
        style.configure("SubBig.TLabel", background="#1f2a52", foreground="#f7f9fc", font=("Segoe UI", 20, "bold"))
        style.configure("Body.TLabel", background="#1c2541", foreground="#f7f9fc", font=("Segoe UI", 11))
        style.configure("SubBody.TLabel", background="#1f2a52", foreground="#f7f9fc", font=("Segoe UI", 11))
        style.configure("Muted.TLabel", background="#1c2541", foreground="#9fb0ca", font=("Segoe UI", 10))
        style.configure("Status.TLabel", background="#0b132b", foreground="#b6c2d9", font=("Segoe UI", 10))

        header = ttk.Frame(self.root, style="Card.TFrame")
        header.pack(fill="x", padx=18, pady=(14, 12))

        ttk.Label(header, text="Smart Air Purifier", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="City:", style="Status.TLabel").pack(side="left", padx=(18, 4))
        ttk.Entry(header, textvariable=self.city_var, width=20).pack(side="left")
        ttk.Label(header, text="Profile:", style="Status.TLabel").pack(side="left", padx=(12, 4))

        profile_box = ttk.Combobox(
            header,
            textvariable=self.profile_var,
            values=["quiet", "balanced", "aggressive"],
            state="readonly",
            width=10,
        )
        profile_box.pack(side="left")

        ttk.Button(header, text="Refresh", command=self.refresh_async).pack(side="left", padx=8)
        self.autotune_btn = ttk.Button(header, text="Autotune", command=self.start_autotune)
        self.autotune_btn.pack(side="left", padx=4)
        ttk.Button(header, text="Settings", command=self.open_settings_window).pack(side="left", padx=4)
        ttk.Button(header, text="Help", command=self.show_help_dialog).pack(side="left", padx=4)

        ttk.Checkbutton(
            header,
            text="AI Auto Fan Mode",
            variable=self.ai_auto_var,
            command=self._on_ai_mode_toggle,
        ).pack(side="left", padx=10)

        self.health_label = tk.Label(
            header,
            text="Health: Unknown",
            bg="#374151",
            fg="#e5e7eb",
            font=("Segoe UI", 10, "bold"),
            padx=10,
            pady=4,
        )
        self.health_label.pack(side="right", padx=(8, 0))

        self.esp_conn_label = tk.Label(
            header,
            text="ESP32: Unknown",
            bg="#2c3e67",
            fg="#dbeafe",
            font=("Segoe UI", 10, "bold"),
            padx=10,
            pady=4,
        )
        self.esp_conn_label.pack(side="right")

        cards = ttk.Frame(self.root, style="Card.TFrame")
        cards.pack(fill="both", expand=True, padx=18, pady=(2, 6))
        cards.columnconfigure(0, weight=1)
        cards.columnconfigure(1, weight=1)
        cards.rowconfigure(0, weight=1, minsize=430)

        self.fan_card = ttk.Frame(cards, style="PrimaryCard.TFrame", padding=16)
        self.fan_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.temp_card = ttk.Frame(cards, style="PrimaryCard.TFrame", padding=16)
        self.temp_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self.fan_card.grid_propagate(False)
        self.temp_card.grid_propagate(False)

        ttk.Label(self.fan_card, text="Air Purifier", style="CardTitle.TLabel").pack(anchor="w")
        self.mode_label = ttk.Label(self.fan_card, text="Mode: --", style="Body.TLabel")
        self.mode_label.pack(anchor="w", pady=(8, 2))
        self.rpm_label = ttk.Label(self.fan_card, text="Fan RPM: --", style="Body.TLabel")
        self.rpm_label.pack(anchor="w", pady=2)

        ttk.Label(self.fan_card, text="CURRENT FAN SPEED", style="CardTitle.TLabel").pack(anchor="w", pady=(8, 0))
        speed_row = ttk.Frame(self.fan_card, style="Card.TFrame")
        speed_row.pack(anchor="w", pady=(2, 8))

        self.current_speed_label = tk.Label(
            speed_row,
            text="--%",
            bg="#1c2541",
            fg="#22c55e",
            font=("Segoe UI", 34, "bold"),
        )
        self.current_speed_label.pack(side="left")

        self.fan_anim_label = tk.Label(
            speed_row,
            text="|",
            bg="#1c2541",
            fg="#7dd3fc",
            font=("Consolas", 26, "bold"),
            padx=12,
        )
        if self.fan_icon_frame is not None:
            self.fan_anim_label.configure(image=self.fan_icon_frame, text="")
        self.fan_anim_label.pack(side="left", pady=(6, 0))

        self.speed_detail_label = ttk.Label(self.fan_card, text="Target: --% | Source: --", style="Muted.TLabel")
        self.speed_detail_label.pack(anchor="w", pady=(0, 6))

        ttk.Separator(self.fan_card, orient="horizontal").pack(fill="x", pady=8)

        self.aqi_label = ttk.Label(self.fan_card, text="AQI: --", style="Body.TLabel")
        self.aqi_label.pack(anchor="w", pady=2)
        self.pollutant_label = ttk.Label(self.fan_card, text="PM2.5 -- | PM10 -- | NO2 --", style="Muted.TLabel")
        self.pollutant_label.pack(anchor="w", pady=2)

        self.ai_fan_label = ttk.Label(self.fan_card, text="AI fan decision: --", style="Body.TLabel")
        self.ai_fan_label.pack(anchor="w", pady=(10, 2))

        self.calibration_label = ttk.Label(self.fan_card, text="Calibration: not tuned", style="Muted.TLabel")
        self.calibration_label.pack(anchor="w", pady=(2, 2))

        self.slider = ttk.Scale(self.fan_card, from_=0, to=100, orient="horizontal", command=self._manual_speed_changed)
        self.slider.pack(fill="x", pady=6)
        if self.ai_auto_var.get():
            self.slider.state(["disabled"])

        self.manual_note = ttk.Label(self.fan_card, text="Manual slider works when ESP mode is MANUAL", style="Muted.TLabel")
        self.manual_note.pack(anchor="w")

        ttk.Separator(self.fan_card, orient="horizontal").pack(fill="x", pady=8)
        ttk.Label(self.fan_card, text="FILTER", style="CardTitle.TLabel").pack(anchor="w")

        self.filter_usage_label = ttk.Label(self.fan_card, text="Filter usage: --", style="Body.TLabel")
        self.filter_usage_label.pack(anchor="w", pady=(4, 2))

        self.filter_left_label = ttk.Label(self.fan_card, text="Filter left: --", style="Muted.TLabel")
        self.filter_left_label.pack(anchor="w", pady=(0, 3))

        self.filter_life_canvas = tk.Canvas(
            self.fan_card,
            height=34,
            bg="#1c2541",
            highlightthickness=1,
            highlightbackground="#2f3e64",
            borderwidth=0,
        )
        self.filter_life_canvas.pack(fill="x", pady=(0, 6))
        self.filter_life_canvas.bind("<Configure>", self._on_filter_life_canvas_configure)

        self.filter_warning_label = tk.Label(
            self.fan_card,
            text="",
            bg="#1c2541",
            fg="#facc15",
            font=("Segoe UI", 10, "bold"),
            wraplength=430,
            justify="left",
        )
        self.filter_warning_label.pack(anchor="w", pady=(0, 4))

        ttk.Button(self.fan_card, text="Reset Filter Usage", command=self.reset_filter_usage).pack(anchor="w", pady=(0, 2))

        ttk.Label(self.temp_card, text="Room Climate", style="CardTitle.TLabel").pack(anchor="w")
        climate_grid = ttk.Frame(self.temp_card, style="Card.TFrame")
        climate_grid.pack(fill="x", pady=(8, 2))
        climate_grid.columnconfigure(0, weight=1)
        climate_grid.columnconfigure(1, weight=1)

        indoor = ttk.Frame(climate_grid, style="SubCard.TFrame", padding=8)
        indoor.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(indoor, text="Indoor", style="SubCardTitle.TLabel").pack(anchor="w")
        self.room_temp_label = ttk.Label(indoor, text="-- C", style="SubBig.TLabel")
        self.room_temp_label.pack(anchor="w", pady=(4, 2))
        self.room_hum_label = ttk.Label(indoor, text="Humidity: -- %", style="SubBody.TLabel")
        self.room_hum_label.pack(anchor="w", pady=2)

        outdoor = ttk.Frame(climate_grid, style="SubCard.TFrame", padding=8)
        outdoor.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ttk.Label(outdoor, text="Outdoor", style="SubCardTitle.TLabel").pack(anchor="w")
        self.out_temp_label = ttk.Label(outdoor, text="-- C", style="SubBig.TLabel")
        self.out_temp_label.pack(anchor="w", pady=(4, 2))
        self.out_hum_label = ttk.Label(outdoor, text="Humidity: -- %", style="SubBody.TLabel")
        self.out_hum_label.pack(anchor="w", pady=2)

        self.out_desc_label = ttk.Label(self.temp_card, text="Conditions: --", style="Muted.TLabel")
        self.out_desc_label.pack(anchor="w", pady=(8, 2))

        ttk.Separator(self.temp_card, orient="horizontal").pack(fill="x", pady=(10, 8))
        ttk.Label(self.temp_card, text="Metric Trends", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(
            self.temp_card,
            text="Grouped trends for temperature and humidity",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 6))

        graph_groups = ttk.Frame(self.temp_card, style="Card.TFrame")
        graph_groups.pack(fill="both", expand=True, pady=(0, 2))

        for idx, group in enumerate(GRAPH_GROUPS):
            group_card = ttk.Frame(graph_groups, style="SubCard.TFrame", padding=8)
            group_card.pack(fill="x", pady=(0, 6 if idx < len(GRAPH_GROUPS) - 1 else 0))

            ttk.Label(group_card, text=group["title"], style="SubCardTitle.TLabel").pack(anchor="w")

            canvas = tk.Canvas(
                group_card,
                height=82,
                bg="#16213e",
                highlightthickness=1,
                highlightbackground="#2f3e64",
                borderwidth=0,
            )
            canvas.pack(fill="x", pady=(4, 4))
            canvas.bind("<Configure>", lambda _event, group_key=group["key"]: self._on_graph_canvas_configure(group_key))
            self.graph_canvases[group["key"]] = canvas

            legend = ttk.Frame(group_card, style="SubCard.TFrame")
            legend.pack(fill="x")
            for col in range(max(1, len(group["metrics"]))):
                legend.columnconfigure(col, weight=1)

            for col, metric_key in enumerate(group["metrics"]):
                spec = GRAPH_METRICS_BY_KEY[metric_key]
                legend_label = tk.Label(
                    legend,
                    text=f"{spec['label']}: --",
                    bg="#1f2a52",
                    fg=spec["color"],
                    font=("Segoe UI", 8, "bold"),
                    anchor="w",
                )
                legend_label.grid(row=0, column=col, sticky="w", padx=(0, 10), pady=(0, 1))
                self.graph_legend_labels[metric_key] = legend_label

        self.footer_card = ttk.Frame(self.root, style="FooterCard.TFrame", padding=12)
        self.footer_card.pack(fill="x", padx=18, pady=(4, 6))
        self.footer_card.columnconfigure(0, weight=1)
        self.footer_card.columnconfigure(1, weight=1)

        air_section = ttk.Frame(self.footer_card, style="FooterCard.TFrame")
        air_section.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(air_section, text="Air Quality", style="CardTitle.TLabel").pack(anchor="w")
        self.ai_air_footer_label = ttk.Label(
            air_section,
            text="--",
            style="Body.TLabel",
            wraplength=420,
            justify="left",
        )
        self.ai_air_footer_label.pack(anchor="w", pady=(6, 0))

        advice_section = ttk.Frame(self.footer_card, style="FooterCard.TFrame")
        advice_section.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ttk.Label(advice_section, text="Weather Notes", style="CardTitle.TLabel").pack(anchor="w")
        self.ai_advice_footer_label = ttk.Label(
            advice_section,
            text="--",
            style="Body.TLabel",
            wraplength=420,
            justify="left",
        )
        self.ai_advice_footer_label.pack(anchor="w", pady=(6, 0))

        status = ttk.Frame(self.root, style="Card.TFrame")
        status.pack(fill="x", padx=18, pady=(4, 10))
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel").pack(side="left")
        ttk.Label(status, textvariable=self.health_summary_var, style="Status.TLabel").pack(side="right")

    def _bind_shortcuts(self) -> None:
        self.root.bind_all("<Control-r>", self._shortcut_refresh)
        self.root.bind_all("<Control-s>", self._shortcut_settings)
        self.root.bind_all("<F1>", self._shortcut_help)

    def _shortcut_refresh(self, _event=None):
        self.refresh_async()
        self._set_status("Manual refresh requested")
        return "break"

    def _shortcut_settings(self, _event=None):
        self.open_settings_window()
        return "break"

    def _shortcut_help(self, _event=None):
        self.show_help_dialog()
        return "break"

    def show_help_dialog(self):
        messagebox.showinfo(
            "Keyboard Shortcuts",
            "Ctrl+R: Refresh now\nCtrl+S: Open settings\nF1: Show this help dialog",
            parent=self.root,
        )

    def _on_close(self) -> None:
        self.shutdown_event.set()
        try:
            self.filter_tracker.flush()
        except Exception:
            self.logger.exception("Failed to flush filter tracker on shutdown")
        self.root.destroy()

    def _tick_fan_animation(self):
        if self.shutdown_event.is_set() or not self.root.winfo_exists():
            return

        if self.fan_icon_base is not None and ImageTk is not None:
            if self.current_speed_pct > 0:
                degrees_per_tick = clamp(4.0 + (self.current_speed_pct * 0.22), 4.0, 26.0)
                self.fan_icon_angle = (self.fan_icon_angle + degrees_per_tick) % 360.0
            rotated = self.fan_icon_base.rotate(-self.fan_icon_angle, resample=PIL_BICUBIC, expand=False)
            self.fan_icon_frame = ImageTk.PhotoImage(rotated)
            self.fan_anim_label.configure(image=self.fan_icon_frame, text="")
        else:
            self.fan_anim_index = (self.fan_anim_index + 1) % len(self.fan_anim_frames)
            self.fan_anim_label.configure(text=self.fan_anim_frames[self.fan_anim_index])

        delay_ms = int(clamp(520 - (self.current_speed_pct * 4), 90, 520))
        self.root.after(delay_ms, self._tick_fan_animation)

    def _on_graph_canvas_configure(self, group_key: str, _event=None):
        self._draw_metric_graph(group_key)

    def _on_filter_life_canvas_configure(self, _event=None):
        self._draw_filter_life_meter()

    def _draw_filter_life_meter(self):
        canvas = self.filter_life_canvas
        if canvas is None:
            return

        width = max(200, canvas.winfo_width())
        height = max(28, canvas.winfo_height())
        canvas.delete("all")

        left_pct = clamp(self.filter_left_pct, 0.0, 100.0)
        fill_fraction = left_pct / 100.0

        pad_x = 7
        pad_y = 6
        body_h = max(12, height - (2 * pad_y))
        cap_w = max(8, int(body_h * 0.55))
        body_x1 = pad_x
        body_y1 = pad_y
        body_x2 = max(body_x1 + 30, width - pad_x - cap_w - 3)
        body_y2 = body_y1 + body_h

        canvas.create_rectangle(
            body_x1,
            body_y1,
            body_x2,
            body_y2,
            fill="#122038",
            outline="#4b5d86",
            width=1,
        )

        inner_pad = 2
        fill_x1 = body_x1 + inner_pad
        fill_y1 = body_y1 + inner_pad
        fill_y2 = body_y2 - inner_pad
        usable_fill_w = max(0, (body_x2 - body_x1) - (2 * inner_pad))
        fill_w = int(round(usable_fill_w * fill_fraction))
        fill_x2 = fill_x1 + fill_w

        if fill_w > 0:
            canvas.create_rectangle(fill_x1, fill_y1, fill_x2, fill_y2, fill=self.filter_life_color, outline="")

        cap_x1 = body_x2 + 2
        cap_y1 = body_y1 + int(body_h * 0.27)
        cap_x2 = cap_x1 + cap_w
        cap_y2 = body_y2 - int(body_h * 0.27)
        canvas.create_rectangle(cap_x1, cap_y1, cap_x2, cap_y2, fill="#2f3e64", outline="#4b5d86", width=1)

        text_color = "#cbd5e1" if left_pct > 8 else "#f8fafc"
        canvas.create_text(
            (body_x1 + body_x2) / 2,
            (body_y1 + body_y2) / 2,
            text=f"{left_pct:.0f}% left",
            fill=text_color,
            font=("Segoe UI", 9, "bold"),
        )

    def _extract_metric_values(self, esp: dict, weather: dict, air: dict) -> dict[str, float]:
        weather_main = weather.get("main") or {}
        air_info = (air.get("list") or [{}])[0]
        air_main = air_info.get("main") or {}
        comps = air_info.get("components") or {}
        return {
            "fan_speed": safe_float(esp.get("speed"), 0.0),
            "fan_rpm": safe_float(esp.get("rpm"), 0.0),
            "aqi": float(safe_int(air_main.get("aqi"), 0)),
            "pm2_5": safe_float(comps.get("pm2_5"), 0.0),
            "pm10": safe_float(comps.get("pm10"), 0.0),
            "no2": safe_float(comps.get("no2"), 0.0),
            "o3": safe_float(comps.get("o3"), 0.0),
            "room_temp": safe_float(esp.get("temp"), 0.0),
            "room_humidity": safe_float(esp.get("humidity"), 0.0),
            "outside_temp": safe_float(weather_main.get("temp"), 0.0),
            "outside_humidity": safe_float(weather_main.get("humidity"), 0.0),
        }

    def _update_metric_history(self, esp: dict, weather: dict, air: dict):
        values = self._extract_metric_values(esp, weather, air)
        for spec in GRAPH_METRICS:
            key = spec["key"]
            val = values.get(key)
            if val is None or (isinstance(val, float) and math.isnan(val)):
                history = self.graph_history[key]
                if history:
                    history.append(history[-1])
                continue
            self.graph_history[key].append(float(val))

    def _draw_metric_graphs(self):
        for group in GRAPH_GROUPS:
            self._draw_metric_graph(group["key"])

    def _draw_metric_graph(self, group_key: str):
        canvas = self.graph_canvases.get(group_key)
        group = GRAPH_GROUPS_BY_KEY.get(group_key)
        if canvas is None or group is None:
            return

        metric_keys = group["metrics"]

        width = max(260, canvas.winfo_width())
        height = max(70, canvas.winfo_height())
        canvas.delete("all")

        pad_x = 10
        pad_y = 8
        usable_w = max(1, width - (2 * pad_x))
        usable_h = max(1, height - (2 * pad_y))

        # Reference guide lines for normalized range.
        for step in range(5):
            y = pad_y + (step * (usable_h / 4.0))
            canvas.create_line(pad_x, y, width - pad_x, y, fill="#253559", width=1)

        any_data = False
        for key in metric_keys:
            spec = GRAPH_METRICS_BY_KEY[key]
            values = list(self.graph_history.get(key, []))
            legend_label = self.graph_legend_labels.get(key)

            if not values:
                if legend_label is not None:
                    legend_label.configure(text=f"{spec['label']}: --")
                continue

            any_data = True
            vmin = min(values)
            vmax = max(values)
            if abs(vmax - vmin) < 1e-6:
                delta = max(1.0, abs(vmax) * 0.05)
                vmin -= delta
                vmax += delta

            denom = max(1, len(values) - 1)
            points: list[float] = []
            for idx, value in enumerate(values):
                x = pad_x + ((idx / denom) * usable_w)
                norm = (value - vmin) / (vmax - vmin)
                y = (height - pad_y) - (norm * usable_h)
                points.extend([x, y])

            if len(points) >= 4:
                canvas.create_line(points, fill=spec["color"], width=2, smooth=True)

            current_value = values[-1]
            if legend_label is not None:
                legend_label.configure(
                    text=f"{spec['label']}: {self._format_metric_value(current_value, spec)}{spec['unit']}"
                )

        if not any_data:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Waiting for metric history...",
                anchor="center",
                fill="#9fb0ca",
                font=("Segoe UI", 10),
            )

    @staticmethod
    def _format_metric_value(value: float, spec: dict) -> str:
        precision = int(spec.get("precision", 1))
        return f"{value:.{precision}f}"

    def _current_config(self, strict: bool = False) -> AppConfig:
        return self.config_manager.create_config(
            city=self.city_var.get(),
            esp_base_url=self.esp_url_var.get(),
            openweather_api_key=self.api_key_var.get(),
            ollama_url=self.ollama_url_var.get(),
            ollama_model=self.model_var.get(),
            control_profile=self.profile_var.get(),
            filter_replacement_hours=self.filter_hours_var.get(),
            fallback=self.config,
            strict=strict,
        )

    def open_settings_window(self):
        window = tk.Toplevel(self.root)
        window.title("Desktop App Settings")
        window.geometry("640x340")
        window.configure(bg="#0b132b")
        window.transient(self.root)

        frame = ttk.Frame(window, style="Card.TFrame", padding=14)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        frame.columnconfigure(1, weight=1)

        fields = [
            ("City", self.city_var),
            ("ESP Base URL", self.esp_url_var),
            ("OpenWeather API Key", self.api_key_var),
            ("Ollama URL", self.ollama_url_var),
            ("Ollama Model", self.model_var),
            ("Filter Replacement Hours", self.filter_hours_var),
        ]

        for idx, (label, var) in enumerate(fields):
            ttk.Label(frame, text=label, style="Body.TLabel").grid(row=idx, column=0, sticky="w", pady=6, padx=(0, 10))
            ttk.Entry(frame, textvariable=var).grid(row=idx, column=1, sticky="ew", pady=6)

        profile_row = len(fields)
        ttk.Label(frame, text="Control Profile", style="Body.TLabel").grid(
            row=profile_row,
            column=0,
            sticky="w",
            pady=6,
            padx=(0, 10),
        )
        ttk.Combobox(
            frame,
            textvariable=self.profile_var,
            values=["quiet", "balanced", "aggressive"],
            state="readonly",
        ).grid(row=profile_row, column=1, sticky="ew", pady=6)

        def save_and_close():
            if self.persist_settings():
                window.destroy()

        buttons = ttk.Frame(frame, style="Card.TFrame")
        buttons.grid(row=profile_row + 1, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Save", command=save_and_close).pack(side="left", padx=6)
        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(side="left", padx=6)

    def persist_settings(self) -> bool:
        try:
            new_config = self._current_config(strict=True)
            self.config_manager.save(new_config)
            self.config = new_config
            self.filter_tracker.set_replacement_interval(new_config.filter_replacement_hours)

            self.city_var.set(new_config.city)
            self.esp_url_var.set(new_config.esp_base_url)
            self.api_key_var.set(new_config.openweather_api_key)
            self.ollama_url_var.set(new_config.ollama_url)
            self.model_var.set(new_config.ollama_model)
            self.profile_var.set(new_config.control_profile)
            self.filter_hours_var.set(str(int(new_config.filter_replacement_hours)))

            self._set_status("Settings saved")
            self.logger.info("Settings persisted")
            return True
        except ValueError as error:
            messagebox.showerror("Invalid settings", str(error), parent=self.root)
            self._set_status(f"Settings not saved: {error}")
            return False
        except Exception as error:
            self.logger.exception("Unexpected settings save error")
            messagebox.showerror("Save error", f"Could not save settings: {error}", parent=self.root)
            self._set_status("Settings save failed")
            return False

    def _schedule_refresh(self):
        if self.shutdown_event.is_set():
            return
        self.refresh_async()
        self.root.after(ESP_REFRESH_INTERVAL_MS, self._schedule_refresh)

    def refresh_async(self):
        if self.autotune_in_progress:
            return

        with self.refresh_lock:
            if self.refresh_in_progress:
                return
            self.refresh_in_progress = True

        thread = threading.Thread(target=self._refresh_worker, daemon=True)
        thread.start()

    def _set_status(self, text: str):
        self.root.after(0, lambda: self.status_var.set(text))

    def start_autotune(self):
        if self.autotune_in_progress:
            self._set_status("Autotune already running")
            return

        self.autotune_in_progress = True
        self.autotune_btn.state(["disabled"])
        threading.Thread(target=self._run_autotune, daemon=True).start()

    def _run_autotune(self):
        previous_speed = None
        previous_auto = None
        config = self._current_config(strict=False)

        try:
            self._set_status("Autotune: preparing")
            esp_state = self.data_manager.read_esp_state(config.esp_base_url)
            self.health_monitor.record_esp_success()

            previous_speed = safe_int(esp_state.get("speed"), 40)
            previous_auto = bool(esp_state.get("auto", False))

            if previous_auto:
                self.data_manager.send_esp_command(config.esp_base_url, "/toggle")

            sweep_points = [20, 30, 40, 50, 60, 70, 80, 90, 100]
            samples = []

            for pwm in sweep_points:
                self._set_status(f"Autotune: testing {pwm}%")
                self.data_manager.send_esp_command(config.esp_base_url, f"/set?speed={pwm}")
                time.sleep(3.0)

                rpm_samples = []
                for _ in range(6):
                    current = self.data_manager.read_esp_state(config.esp_base_url)
                    rpm = safe_int(current.get("rpm"), 0)
                    if 0 <= rpm <= FAN_APP_MAX_RPM:
                        rpm_samples.append(rpm)
                    time.sleep(0.35)

                avg_rpm = int(round(sum(rpm_samples) / len(rpm_samples))) if rpm_samples else 0
                samples.append({"pwm": pwm, "rpm": avg_rpm})

            monotonic_samples = []
            max_seen = 0
            for sample in samples:
                max_seen = max(max_seen, sample["rpm"])
                monotonic_samples.append({"pwm": int(sample["pwm"]), "rpm": int(max_seen)})

            spin_up = next((sample for sample in monotonic_samples if sample["rpm"] >= 250), monotonic_samples[0])
            max_sample = max(monotonic_samples, key=lambda sample: sample["rpm"])

            calibration = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "samples": monotonic_samples,
                "spin_up_pwm": int(spin_up["pwm"]),
                "spin_up_rpm": int(spin_up["rpm"]),
                "max_rpm": int(clamp(max_sample["rpm"], 0, FAN_APP_MAX_RPM)),
            }

            self.calibration_manager.save_calibration(calibration)
            self._set_status(
                f"Autotune done: spin-up {calibration['spin_up_pwm']}%, max {calibration['max_rpm']} RPM"
            )
        except Exception as error:
            self.logger.exception("Autotune failed")
            self.health_monitor.record_esp_failure(str(error))
            self._set_status(f"Autotune failed: {error}")
        finally:
            try:
                if previous_speed is not None:
                    self.data_manager.send_esp_command(config.esp_base_url, f"/set?speed={previous_speed}")
                if previous_auto:
                    state = self.data_manager.read_esp_state(config.esp_base_url)
                    if not state.get("auto", False):
                        self.data_manager.send_esp_command(config.esp_base_url, "/toggle")
                self.last_esp = self.data_manager.read_esp_state(config.esp_base_url)
            except Exception:
                self.logger.exception("Failed to restore state after autotune")

            self.autotune_in_progress = False
            self.root.after(0, lambda: self.autotune_btn.state(["!disabled"]))
            self.root.after(0, self._update_calibration_label)
            self.refresh_async()

    def _refresh_worker(self):
        config = self._current_config(strict=False)
        local_fail_safe = False
        weather_error = None
        used_cached_weather = False

        try:
            try:
                esp = self.data_manager.read_esp_state(config.esp_base_url)
                self.last_esp = esp
                self.health_monitor.record_esp_success()
                self.root.after(0, lambda: self._set_esp_indicator(True))
            except Exception as error:
                self.health_monitor.record_esp_failure(str(error))
                self.root.after(0, lambda: self._set_esp_indicator(False))
                self._set_status("ESP32 is unreachable. Check power/network.")
                return

            now_ts = time.time()
            weather = self.last_weather
            air = self.last_air
            should_fetch_weather = (
                weather is None
                or air is None
                or (now_ts - self.last_weather_fetch_ts) >= WEATHER_REFRESH_INTERVAL_SECONDS
            )

            if should_fetch_weather:
                try:
                    weather, air = self.data_manager.read_openweather(config.city, config.openweather_api_key)
                    self.last_weather = weather
                    self.last_air = air
                    self.last_weather_fetch_ts = now_ts
                    self.health_monitor.record_api_success()
                    self.fail_safe_mode = False
                except Exception as error:
                    weather_error = error
                    weather = self.last_weather
                    air = self.last_air
                    local_fail_safe = True
                    self.fail_safe_mode = True
                    self.health_monitor.record_api_failure(str(error))
                    used_cached_weather = weather is not None and air is not None
            else:
                used_cached_weather = True

            if weather is None or air is None:
                self._set_status("Weather service unavailable and no cached data yet.")
                return

            fan_ai_speed = None
            profile_name = config.control_profile if config.control_profile in PROFILE_CONFIG else "aggressive"
            profile = PROFILE_CONFIG[profile_name]

            if self.ai_auto_var.get():
                air_info = (air.get("list") or [{}])[0]
                components = air_info.get("components") or {}
                air_main = safe_int((air_info.get("main") or {}).get("aqi"), 3)

                baseline = self.ai_controller.curve_baseline_speed(
                    aqi=air_main,
                    components=components,
                    profile=profile,
                    calibration_manager=self.calibration_manager,
                )

                ai_target, ai_failed = self.ai_controller.decide_fan_target(
                    config=config,
                    profile_name=profile_name,
                    esp=esp,
                    weather=weather,
                    air=air,
                    baseline=baseline,
                    force_fail_safe=local_fail_safe,
                )

                if ai_failed:
                    local_fail_safe = True
                    self.fail_safe_mode = True

                current_speed = safe_int(esp.get("speed"), 0)
                applied_speed, _target = self.fan_controller.compute_applied_speed(ai_target, current_speed, profile)

                if self.fan_controller.should_push(current_speed):
                    try:
                        esp = self._ensure_manual_and_set_speed(config, esp, applied_speed)
                        self.health_monitor.record_esp_success()
                        self.last_esp = esp
                    except Exception as error:
                        self.health_monitor.record_esp_failure(str(error))
                        self._set_status("Unable to apply AI fan speed to ESP32.")

                fan_ai_speed = applied_speed
            else:
                self.fan_controller.reset_ai_state()

            advice = self.ai_controller.temperature_advice(config, esp, weather, force_fail_safe=local_fail_safe)
            pollution_comment = self.ai_controller.pollution_comment(config, air, force_fail_safe=local_fail_safe)

            speed_for_filter = safe_int(esp.get("speed"), 0)
            filter_state = self.filter_tracker.update_runtime(speed_for_filter)

            self.data_logger.log_csv_row(
                profile_name=profile_name,
                fail_safe=local_fail_safe,
                esp=esp,
                weather=weather,
                air=air,
                fan_ai_speed=fan_ai_speed,
                fan_ai_target=self.fan_controller.ai_target_speed,
            )

            self.root.after(
                0,
                lambda: self._update_ui(
                    esp=esp,
                    weather=weather,
                    air=air,
                    fan_ai_speed=fan_ai_speed,
                    advice=advice,
                    pollution_comment=pollution_comment,
                    filter_state=filter_state,
                    profile_name=profile_name,
                ),
            )

            if local_fail_safe and weather_error is not None:
                self._set_status("Fail-safe mode: using cached weather data")
            elif used_cached_weather:
                self._set_status(f"Updated at {time.strftime('%H:%M:%S')} (cached weather)")
            else:
                self._set_status(f"Updated at {time.strftime('%H:%M:%S')}")
        except Exception as error:
            self.logger.exception("Refresh worker failed")
            self._set_status(f"Update error: {error}")
        finally:
            self.root.after(0, self._update_health_indicator)
            with self.refresh_lock:
                self.refresh_in_progress = False

    def _ensure_manual_and_set_speed(self, config: AppConfig, esp: dict, speed: int) -> dict:
        if esp.get("auto"):
            esp = self.data_manager.send_esp_command(config.esp_base_url, "/toggle")
        confirm = self.data_manager.send_esp_command(config.esp_base_url, f"/set?speed={speed}")
        self.fan_controller.mark_push()
        return confirm

    def _set_esp_indicator(self, online: bool):
        if online:
            self.esp_conn_label.configure(text="ESP32: Online", bg="#14532d", fg="#dcfce7")
        else:
            self.esp_conn_label.configure(text="ESP32: Offline", bg="#7f1d1d", fg="#fecaca")

    def _update_health_indicator(self):
        status = self.health_monitor.status()
        self.health_label.configure(text=status.label, bg=status.background, fg=status.foreground)
        self.health_summary_var.set(status.summary)

    def _update_calibration_label(self):
        calibration = self.calibration_manager.get_calibration()
        if calibration and isinstance(calibration.get("samples"), list):
            spin_up = calibration.get("spin_up_pwm", "--")
            max_rpm = calibration.get("max_rpm", "--")
            self.calibration_label.configure(text=f"Calibration: tuned (spin-up {spin_up}%, max {max_rpm} RPM)")
        else:
            self.calibration_label.configure(text="Calibration: not tuned")

    def _manual_speed_changed(self, value):
        try:
            speed = int(clamp(float(value), 0, 100))
            self.current_speed_pct = speed
            self.current_speed_label.configure(text=f"{speed}%")

            if self.slider_syncing:
                return
            if self.ai_auto_var.get():
                return

            should_start_worker = False
            with self.manual_state_lock:
                self.manual_pending_speed = speed
                # Hold slider/UI briefly to avoid refresh using stale ESP speed.
                self.manual_override_until = time.time() + 1.5
                if not self.manual_sender_running:
                    self.manual_sender_running = True
                    should_start_worker = True

            if should_start_worker:
                threading.Thread(target=self._manual_send_worker, daemon=True).start()
        except Exception:
            self.logger.exception("Manual speed change failed")

    def _manual_send_worker(self):
        while True:
            with self.manual_state_lock:
                speed = self.manual_pending_speed
                self.manual_pending_speed = None

            if speed is None:
                with self.manual_state_lock:
                    if self.manual_pending_speed is None:
                        self.manual_sender_running = False
                        return
                    continue

            self._send_manual_speed(speed)

    def _send_manual_speed(self, speed: int):
        config = self._current_config(strict=False)
        try:
            esp = self.data_manager.read_esp_state(config.esp_base_url)
            if esp.get("auto"):
                esp = self.data_manager.send_esp_command(config.esp_base_url, "/toggle")
            confirm = self.data_manager.send_esp_command(config.esp_base_url, f"/set?speed={speed}")
            self.last_esp = confirm
            self.health_monitor.record_esp_success()

            confirmed_speed = int(clamp(safe_int(confirm.get("speed"), speed), 0, 100))
            sequence = confirm.get("cmd_seq", "-")

            with self.manual_state_lock:
                if self.manual_pending_speed is None:
                    self.manual_override_until = time.time() + 1.0

            self.root.after(0, lambda s=confirmed_speed: self._apply_manual_speed_to_ui(s))
            self._set_status(f"Manual speed set: {confirmed_speed}% (seq {sequence})")
        except Exception as error:
            self.health_monitor.record_esp_failure(str(error))
            self._set_status("Manual speed update failed. Check ESP32 connection.")

    def _apply_manual_speed_to_ui(self, speed: int):
        if self.ai_auto_var.get():
            return
        speed = int(clamp(speed, 0, 100))
        self.current_speed_pct = speed
        self.current_speed_label.configure(text=f"{speed}%")
        self.slider_syncing = True
        self.slider.set(speed)
        self.slider_syncing = False

    def _on_ai_mode_toggle(self):
        if self.ai_auto_var.get():
            self.fan_controller.reset_ai_state()
            self.fail_safe_mode = False
            with self.manual_state_lock:
                self.manual_pending_speed = None
                self.manual_override_until = 0.0
            self.slider.state(["disabled"])
            self._set_status("AI auto fan mode enabled")
            self.refresh_async()
            return

        self.slider.state(["!disabled"])
        threading.Thread(target=self._force_manual_mode, daemon=True).start()

    def _force_manual_mode(self):
        config = self._current_config(strict=False)
        try:
            esp = self.data_manager.read_esp_state(config.esp_base_url)
            if esp.get("auto"):
                self.data_manager.send_esp_command(config.esp_base_url, "/toggle")
            self.health_monitor.record_esp_success()
            self._set_status("AI mode off: ESP set to MANUAL")
        except Exception as error:
            self.health_monitor.record_esp_failure(str(error))
            self._set_status("Could not switch ESP to manual mode")

    def _update_ui(
        self,
        esp: dict,
        weather: dict,
        air: dict,
        fan_ai_speed: int | None,
        advice: str,
        pollution_comment: str,
        filter_state: FilterState,
        profile_name: str,
    ):
        outside = weather.get("main") or {}
        weather_desc = str((weather.get("weather") or [{"description": "--"}])[0].get("description", "--")).title()
        air_info = (air.get("list") or [{}])[0]
        components = air_info.get("components") or {}
        air_main = safe_int((air_info.get("main") or {}).get("aqi"), 0)

        mode_text = "AUTO" if bool(esp.get("auto")) else "MANUAL"
        sequence = esp.get("cmd_seq", "-")

        self.mode_label.configure(text=f"Mode: {mode_text} | Profile: {profile_name} | Seq: {sequence}")
        self.rpm_label.configure(text=f"Fan RPM: {esp.get('rpm', '--')}")

        current_speed = safe_int(esp.get("speed"), 0)
        display_speed = int(clamp(current_speed, 0, 100))

        if not self.ai_auto_var.get():
            with self.manual_state_lock:
                pending_speed = self.manual_pending_speed
                override_until = self.manual_override_until
            if time.time() < override_until:
                if pending_speed is not None:
                    display_speed = int(clamp(pending_speed, 0, 100))
                else:
                    display_speed = int(clamp(self.current_speed_pct, 0, 100))

        self.current_speed_pct = display_speed
        self.current_speed_label.configure(text=f"{display_speed}%")

        self.slider_syncing = True
        self.slider.set(display_speed)
        self.slider_syncing = False

        if self.ai_auto_var.get():
            self.slider.state(["disabled"])
        else:
            self.slider.state(["!disabled"])

        self.aqi_label.configure(text=f"AQI: {air_main} ({aqi_label(air_main)})")
        self.pollutant_label.configure(
            text=(
                f"PM2.5 {components.get('pm2_5', 0):.1f} | "
                f"PM10 {components.get('pm10', 0):.1f} | "
                f"NO2 {components.get('no2', 0):.1f} | "
                f"O3 {components.get('o3', 0):.1f}"
            )
        )

        if fan_ai_speed is not None and self.ai_auto_var.get():
            target_text = self.fan_controller.ai_target_speed if self.fan_controller.ai_target_speed is not None else fan_ai_speed
            source = "AI (fail-safe)" if self.fail_safe_mode else "AI"
            self.ai_fan_label.configure(text=f"AI fan decision: {fan_ai_speed}% (target {target_text}%, {source})")
            self.speed_detail_label.configure(text=f"Target: {target_text}% | Source: {source}")
        elif not self.ai_auto_var.get():
            self.ai_fan_label.configure(text="AI fan decision: off")
            self.speed_detail_label.configure(text="Target: manual slider | Source: manual")

        self.ai_air_footer_label.configure(text=pollution_comment or "--")
        self.ai_advice_footer_label.configure(text=advice or "--")

        self.room_temp_label.configure(text=f"{esp.get('temp', '--')} C")
        self.room_hum_label.configure(text=f"Humidity: {esp.get('humidity', '--')} %")
        self.out_temp_label.configure(text=f"{outside.get('temp', '--')} C")
        self.out_hum_label.configure(text=f"Humidity: {outside.get('humidity', '--')} %")
        self.out_desc_label.configure(text=f"Conditions: {weather_desc}")

        self._update_metric_history(esp, weather, air)
        self._draw_metric_graphs()
        self._update_filter_labels(filter_state)
        self._update_calibration_label()

    def _update_filter_labels(self, filter_state: FilterState):
        usage_pct = self.filter_tracker.usage_percent(filter_state)
        left_pct = clamp(100.0 - usage_pct, 0.0, 100.0)
        hours_left = max(0.0, filter_state.replacement_interval_hours - filter_state.runtime_hours)
        usage_text = (
            f"Filter usage: {filter_state.runtime_hours:.1f}h / "
            f"{filter_state.replacement_interval_hours:.0f}h ({usage_pct:.0f}%)"
        )
        self.filter_usage_label.configure(text=usage_text)
        if self.filter_left_label is not None:
            self.filter_left_label.configure(text=f"Filter left: {left_pct:.0f}% ({hours_left:.0f}h)")

        if left_pct <= 5:
            self.filter_life_color = "#ef4444"
        elif left_pct <= 20:
            self.filter_life_color = "#facc15"
        else:
            self.filter_life_color = "#22c55e"
        self.filter_left_pct = left_pct
        self._draw_filter_life_meter()

        if usage_pct >= 100:
            self.filter_warning_label.configure(
                text="Filter replacement overdue. Install a new filter and reset usage.",
                fg="#facc15",
            )
        elif usage_pct >= 80:
            self.filter_warning_label.configure(
                text="Filter is approaching replacement threshold.",
                fg="#facc15",
            )
        else:
            self.filter_warning_label.configure(
                text="Filter condition is normal.",
                fg="#86efac",
            )

    def reset_filter_usage(self):
        if not messagebox.askyesno("Reset Filter", "Reset filter usage after installing a new filter?", parent=self.root):
            return
        state = self.filter_tracker.reset()
        self._update_filter_labels(state)
        self._set_status("Filter usage reset")


if __name__ == "__main__":
    app_root = tk.Tk()
    app = App(app_root)
    app_root.mainloop()
