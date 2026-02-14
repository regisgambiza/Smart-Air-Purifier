import json
import math
import os
import re
import threading
import time
import tkinter as tk
import csv
from datetime import datetime, timezone
from tkinter import ttk
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from urllib.error import HTTPError


SETTINGS_FILE = "desktop_app_settings.json"
DEFAULT_OPENWEATHER_API_KEY = "56672a7fddd6d20e51a88155f0b4a0f2"
DEFAULT_ESP_BASE_URL = "http://192.168.1.132"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
DEFAULT_OLLAMA_MODEL = "llama3.1:8b"
DEFAULT_CITY = "San Jose"


def http_get_json(url: str, timeout: int = 8):
    req = Request(url, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {body or e.reason}") from e


def http_get_text(url: str, timeout: int = 8):
    req = Request(url, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def ollama_generate(prompt: str, model: str, ollama_url: str, timeout: int = 20) -> str:
    payload = json.dumps(
        {"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0.2}}
    ).encode("utf-8")
    req = Request(ollama_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("response", "").strip()


def extract_speed(text: str) -> int:
    m = re.search(r"(\d{1,3})", text)
    if not m:
        return 55
    return max(0, min(100, int(m.group(1))))


def aqi_label(aqi: int) -> str:
    return {
        1: "Good",
        2: "Fair",
        3: "Moderate",
        4: "Poor",
        5: "Very Poor",
    }.get(aqi, "Unknown")


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


LOG_FILE = "air_purifier_timeseries.csv"
CALIBRATION_FILE = "fan_calibration.json"
FAN_APP_MAX_RPM = 2200

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


def load_settings() -> dict:
    defaults = {
        "city": DEFAULT_CITY,
        "esp_base_url": DEFAULT_ESP_BASE_URL,
        "openweather_api_key": DEFAULT_OPENWEATHER_API_KEY,
        "ollama_url": DEFAULT_OLLAMA_URL,
        "ollama_model": DEFAULT_OLLAMA_MODEL,
        "control_profile": "aggressive",
    }
    if not os.path.exists(SETTINGS_FILE):
        return defaults
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        defaults.update({k: v for k, v in data.items() if isinstance(v, str)})
    except Exception:
        pass
    return defaults


def save_settings(settings: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def load_calibration() -> dict | None:
    if not os.path.exists(CALIBRATION_FILE):
        return None
    try:
        with open(CALIBRATION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        if "samples" not in data or not isinstance(data["samples"], list):
            return None
        cleaned = []
        max_seen = 0
        for s in data["samples"]:
            try:
                pwm = int(s.get("pwm", 0))
                rpm = int(s.get("rpm", 0))
            except Exception:
                continue
            pwm = int(clamp(pwm, 0, 100))
            rpm = int(clamp(rpm, 0, FAN_APP_MAX_RPM))
            max_seen = max(max_seen, rpm)
            cleaned.append({"pwm": pwm, "rpm": max_seen})
        if not cleaned:
            return None
        data["samples"] = cleaned
        data["max_rpm"] = int(clamp(int(data.get("max_rpm", max_seen)), 0, FAN_APP_MAX_RPM))
        data["spin_up_rpm"] = int(clamp(int(data.get("spin_up_rpm", cleaned[0]["rpm"])), 0, FAN_APP_MAX_RPM))
        if data["max_rpm"] < data["spin_up_rpm"]:
            data["max_rpm"] = data["spin_up_rpm"]
        return data
    except Exception:
        return None


def save_calibration(cal: dict):
    with open(CALIBRATION_FILE, "w", encoding="utf-8") as f:
        json.dump(cal, f, indent=2)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Smart Air Purifier Desktop")
        self.root.geometry("920x520")
        self.root.configure(bg="#0b132b")

        settings = load_settings()
        profile_value = settings.get("control_profile", "aggressive").lower()
        if profile_value not in PROFILE_CONFIG:
            profile_value = "aggressive"
        self.city_var = tk.StringVar(value=settings["city"])
        self.esp_url_var = tk.StringVar(value=settings["esp_base_url"])
        self.api_key_var = tk.StringVar(value=settings["openweather_api_key"])
        self.ollama_url_var = tk.StringVar(value=settings["ollama_url"])
        self.model_var = tk.StringVar(value=settings["ollama_model"])
        self.profile_var = tk.StringVar(value=profile_value)
        self.ai_auto_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Ready")
        self.last_advice_ts = 0.0
        self.last_pollution_comment_ts = 0.0
        self.last_fan_ai_ts = 0.0
        self.last_manual_send_ts = 0.0
        self.last_ai_push_ts = 0.0
        self.slider_syncing = False
        self.esp_auto_mode = True
        self.ai_target_speed = None
        self.ai_applied_speed = None
        self.last_esp = None
        self.last_weather = None
        self.last_air = None
        self.fail_safe_mode = False
        self.refresh_in_progress = False
        self.refresh_lock = threading.Lock()
        self.autotune_in_progress = False
        self.calibration = load_calibration()

        self._build_ui()
        self._schedule_refresh()

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Card.TFrame", background="#1c2541")
        style.configure("Title.TLabel", background="#0b132b", foreground="#f7f9fc", font=("Segoe UI", 18, "bold"))
        style.configure("CardTitle.TLabel", background="#1c2541", foreground="#b6c2d9", font=("Segoe UI", 10, "bold"))
        style.configure("Big.TLabel", background="#1c2541", foreground="#f7f9fc", font=("Segoe UI", 20, "bold"))
        style.configure("Body.TLabel", background="#1c2541", foreground="#f7f9fc", font=("Segoe UI", 11))
        style.configure("Muted.TLabel", background="#1c2541", foreground="#9fb0ca", font=("Segoe UI", 10))
        style.configure("Status.TLabel", background="#0b132b", foreground="#b6c2d9", font=("Segoe UI", 10))

        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=18, pady=(14, 10))
        top.configure(style="Card.TFrame")

        header = ttk.Frame(self.root, style="Card.TFrame")
        header.pack(fill="x", padx=18, pady=(0, 12))
        ttk.Label(header, text="Smart Air Purifier", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="City:", style="Status.TLabel").pack(side="left", padx=(18, 4))
        city_entry = ttk.Entry(header, textvariable=self.city_var, width=20)
        city_entry.pack(side="left")
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
        ttk.Checkbutton(
            header,
            text="AI Auto Fan Mode",
            variable=self.ai_auto_var,
            command=self._on_ai_mode_toggle,
        ).pack(side="left", padx=10)
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
        cards.pack(fill="both", expand=True, padx=18, pady=4)
        cards.columnconfigure(0, weight=1)
        cards.columnconfigure(1, weight=1)
        cards.rowconfigure(0, weight=1)

        self.fan_card = ttk.Frame(cards, style="Card.TFrame", padding=16)
        self.fan_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.temp_card = ttk.Frame(cards, style="Card.TFrame", padding=16)
        self.temp_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        ttk.Label(self.fan_card, text="Air Purifier", style="CardTitle.TLabel").pack(anchor="w")
        self.mode_label = ttk.Label(self.fan_card, text="Mode: --", style="Body.TLabel")
        self.mode_label.pack(anchor="w", pady=(8, 2))
        self.rpm_label = ttk.Label(self.fan_card, text="Fan RPM: --", style="Body.TLabel")
        self.rpm_label.pack(anchor="w", pady=2)
        ttk.Label(self.fan_card, text="CURRENT FAN SPEED", style="CardTitle.TLabel").pack(anchor="w", pady=(8, 0))
        self.current_speed_label = tk.Label(
            self.fan_card,
            text="--%",
            bg="#1c2541",
            fg="#22c55e",
            font=("Segoe UI", 34, "bold"),
        )
        self.current_speed_label.pack(anchor="w", pady=(2, 8))
        self.speed_detail_label = ttk.Label(self.fan_card, text="Target: --% | Source: --", style="Muted.TLabel")
        self.speed_detail_label.pack(anchor="w", pady=(0, 6))
        ttk.Separator(self.fan_card, orient="horizontal").pack(fill="x", pady=8)
        self.aqi_label = ttk.Label(self.fan_card, text="AQI: --", style="Body.TLabel")
        self.aqi_label.pack(anchor="w", pady=2)
        self.pollutant_label = ttk.Label(self.fan_card, text="PM2.5 -- | PM10 -- | NO2 --", style="Muted.TLabel")
        self.pollutant_label.pack(anchor="w", pady=2)
        self.ai_air_label = ttk.Label(
            self.fan_card,
            text="Air insight: --",
            style="Body.TLabel",
            wraplength=390,
            justify="left",
        )
        self.ai_air_label.pack(anchor="w", pady=(8, 2))
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

        ttk.Label(self.temp_card, text="Room Climate", style="CardTitle.TLabel").pack(anchor="w")
        climate_grid = ttk.Frame(self.temp_card, style="Card.TFrame")
        climate_grid.pack(fill="x", pady=(8, 2))
        climate_grid.columnconfigure(0, weight=1)
        climate_grid.columnconfigure(1, weight=1)

        indoor = ttk.Frame(climate_grid, style="Card.TFrame")
        indoor.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(indoor, text="Indoor", style="CardTitle.TLabel").pack(anchor="w")
        self.room_temp_label = ttk.Label(indoor, text="-- C", style="Big.TLabel")
        self.room_temp_label.pack(anchor="w", pady=(4, 2))
        self.room_hum_label = ttk.Label(indoor, text="Humidity: -- %", style="Body.TLabel")
        self.room_hum_label.pack(anchor="w", pady=2)

        outdoor = ttk.Frame(climate_grid, style="Card.TFrame")
        outdoor.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ttk.Label(outdoor, text="Outdoor", style="CardTitle.TLabel").pack(anchor="w")
        self.out_temp_label = ttk.Label(outdoor, text="-- C", style="Big.TLabel")
        self.out_temp_label.pack(anchor="w", pady=(4, 2))
        self.out_hum_label = ttk.Label(outdoor, text="Humidity: -- %", style="Body.TLabel")
        self.out_hum_label.pack(anchor="w", pady=2)
        self.out_desc_label = ttk.Label(self.temp_card, text="Conditions: --", style="Muted.TLabel")
        self.out_desc_label.pack(anchor="w", pady=(8, 2))
        ttk.Separator(self.temp_card, orient="horizontal").pack(fill="x", pady=10)
        self.ai_advice_label = ttk.Label(
            self.temp_card,
            text="AI advice: --",
            style="Body.TLabel",
            wraplength=390,
            justify="left",
        )
        self.ai_advice_label.pack(anchor="w")

        status = ttk.Frame(self.root, style="Card.TFrame")
        status.pack(fill="x", padx=18, pady=(4, 10))
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel").pack(anchor="w")

    def open_settings_window(self):
        win = tk.Toplevel(self.root)
        win.title("Desktop App Settings")
        win.geometry("560x280")
        win.configure(bg="#0b132b")
        win.transient(self.root)

        frame = ttk.Frame(win, style="Card.TFrame", padding=14)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        frame.columnconfigure(1, weight=1)

        fields = [
            ("City", self.city_var),
            ("ESP Base URL", self.esp_url_var),
            ("OpenWeather API Key", self.api_key_var),
            ("Ollama URL", self.ollama_url_var),
            ("Ollama Model", self.model_var),
        ]

        for i, (label, var) in enumerate(fields):
            ttk.Label(frame, text=label, style="Body.TLabel").grid(row=i, column=0, sticky="w", pady=6, padx=(0, 10))
            ttk.Entry(frame, textvariable=var).grid(row=i, column=1, sticky="ew", pady=6)

        profile_row = len(fields)
        ttk.Label(frame, text="Control Profile", style="Body.TLabel").grid(
            row=profile_row, column=0, sticky="w", pady=6, padx=(0, 10)
        )
        ttk.Combobox(
            frame,
            textvariable=self.profile_var,
            values=["quiet", "balanced", "aggressive"],
            state="readonly",
        ).grid(row=profile_row, column=1, sticky="ew", pady=6)

        def save_and_close():
            self.persist_settings()
            self.status_var.set("Settings saved")
            win.destroy()

        buttons = ttk.Frame(frame, style="Card.TFrame")
        buttons.grid(row=profile_row + 1, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Save", command=save_and_close).pack(side="left", padx=6)
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="left", padx=6)

    def persist_settings(self):
        save_settings(
            {
                "city": self.city_var.get().strip(),
                "esp_base_url": self.esp_url_var.get().strip(),
                "openweather_api_key": self.api_key_var.get().strip(),
                "ollama_url": self.ollama_url_var.get().strip(),
                "ollama_model": self.model_var.get().strip(),
                "control_profile": self.profile_var.get().strip().lower(),
            }
        )

    def _schedule_refresh(self):
        self.refresh_async()
        self.root.after(12000, self._schedule_refresh)

    def refresh_async(self):
        if self.autotune_in_progress:
            return
        with self.refresh_lock:
            if self.refresh_in_progress:
                return
            self.refresh_in_progress = True
        t = threading.Thread(target=self._refresh_worker, daemon=True)
        t.start()

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
        prev_speed = None
        prev_auto = None
        try:
            self._set_status("Autotune: preparing")
            esp0 = self._read_esp()
            prev_speed = int(esp0.get("speed", 40))
            prev_auto = bool(esp0.get("auto", False))

            # Ensure MANUAL for sweep control and disable AI UI mode during tuning.
            if prev_auto:
                self._send_esp_command_json("/toggle")

            sweep_points = [20, 30, 40, 50, 60, 70, 80, 90, 100]
            samples = []
            for pwm in sweep_points:
                self._set_status(f"Autotune: testing {pwm}%")
                self._send_esp_command_json(f"/set?speed={pwm}")
                time.sleep(3.0)  # settle

                rpms = []
                for _ in range(6):
                    st = self._read_esp()
                    rpm = int(st.get("rpm", 0))
                    if 0 <= rpm <= FAN_APP_MAX_RPM:
                        rpms.append(rpm)
                    time.sleep(0.35)
                avg_rpm = int(round(sum(rpms) / len(rpms))) if rpms else 0
                samples.append({"pwm": pwm, "rpm": avg_rpm})

            # Enforce monotonic RPM curve to reduce noise effect.
            monotonic = []
            max_seen = 0
            for s in samples:
                max_seen = max(max_seen, int(s["rpm"]))
                monotonic.append({"pwm": int(s["pwm"]), "rpm": int(max_seen)})

            spin_up = next((s for s in monotonic if s["rpm"] >= 250), monotonic[0])
            max_s = max(monotonic, key=lambda x: x["rpm"])
            cal = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "samples": monotonic,
                "spin_up_pwm": int(spin_up["pwm"]),
                "spin_up_rpm": int(spin_up["rpm"]),
                "max_rpm": int(clamp(int(max_s["rpm"]), 0, FAN_APP_MAX_RPM)),
            }
            self.calibration = cal
            save_calibration(cal)

            self._set_status(
                f"Autotune done: spin-up {cal['spin_up_pwm']}%, max {cal['max_rpm']} RPM"
            )
        except Exception as e:
            self._set_status(f"Autotune failed: {e}")
        finally:
            # Restore previous behavior.
            try:
                if prev_speed is not None:
                    self._send_esp_command_json(f"/set?speed={prev_speed}")
                if prev_auto:
                    st = self._read_esp()
                    if not st.get("auto", False):
                        self._send_esp_command_json("/toggle")
                self.last_esp = self._read_esp()
            except Exception:
                pass

            self.autotune_in_progress = False
            self.root.after(0, lambda: self.autotune_btn.state(["!disabled"]))
            self.refresh_async()

    def _refresh_worker(self):
        try:
            try:
                esp = self._read_esp()
                self.last_esp = esp
                self.root.after(0, lambda: self._set_esp_indicator(True))
            except Exception as e:
                self.root.after(0, lambda: self._set_esp_indicator(False))
                self._set_status(f"ESP connection error: {e}")
                return

            weather = None
            air = None
            weather_error = None
            try:
                weather, air = self._read_openweather(self.city_var.get().strip())
                self.last_weather = weather
                self.last_air = air
                self.fail_safe_mode = False
            except Exception as e:
                weather_error = e
                weather = self.last_weather
                air = self.last_air
                self.fail_safe_mode = True

            if weather is None or air is None:
                self._set_status(f"Update error: {weather_error}")
                return

            try:
                fan_ai_speed, advice, pollution_comment = self._ai_actions(esp, weather, air, self.fail_safe_mode)
                esp_ui = self.last_esp if isinstance(self.last_esp, dict) else esp
                self._log_row(esp_ui, weather, air, fan_ai_speed)
                self.root.after(0, lambda: self._update_ui(esp_ui, weather, air, fan_ai_speed, advice, pollution_comment))
                if self.fail_safe_mode:
                    self._set_status(f"Fail-safe active (weather): {weather_error}")
                else:
                    self._set_status(f"Updated at {time.strftime('%H:%M:%S')}")
            except Exception as e:
                self._set_status(f"Control update error: {e}")
        finally:
            with self.refresh_lock:
                self.refresh_in_progress = False

    def _set_esp_indicator(self, online: bool):
        if online:
            self.esp_conn_label.configure(text="ESP32: Online", bg="#14532d", fg="#dcfce7")
        else:
            self.esp_conn_label.configure(text="ESP32: Offline", bg="#7f1d1d", fg="#fecaca")

    def _log_row(self, esp: dict, weather: dict, air: dict, fan_ai_speed):
        exists = os.path.exists(LOG_FILE)
        air_info = air["list"][0]
        comps = air_info["components"]
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "profile": self.profile_var.get().strip().lower(),
            "fail_safe": int(self.fail_safe_mode),
            "aqi": air_info["main"]["aqi"],
            "pm2_5": round(float(comps.get("pm2_5", 0.0)), 2),
            "pm10": round(float(comps.get("pm10", 0.0)), 2),
            "fan_speed_reported": esp.get("speed"),
            "fan_speed_ai_applied": fan_ai_speed if fan_ai_speed is not None else "",
            "fan_speed_ai_target": self.ai_target_speed if self.ai_target_speed is not None else "",
            "room_temp_c": esp.get("temp"),
            "room_humidity_pct": esp.get("humidity"),
            "outside_temp_c": weather["main"].get("temp"),
            "outside_humidity_pct": weather["main"].get("humidity"),
            "cmd_seq": esp.get("cmd_seq", ""),
            "last_cmd_ms": esp.get("last_cmd_ms", ""),
        }
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if not exists:
                writer.writeheader()
            writer.writerow(row)

    def _esp_base_url(self):
        return self.esp_url_var.get().strip().rstrip("/")

    def _read_esp(self):
        try:
            return http_get_json(f"{self._esp_base_url()}/state")
        except Exception:
            return http_get_json(f"{self._esp_base_url()}/data")

    def _send_esp_command_json(self, path: str):
        try:
            return http_get_json(f"{self._esp_base_url()}{path}")
        except Exception:
            http_get_text(f"{self._esp_base_url()}{path}")
            return self._read_esp()

    def _read_openweather(self, city: str):
        api_key = self.api_key_var.get().strip()
        if not api_key:
            raise RuntimeError("OpenWeather API key is empty")
        city_q = quote_plus(city)
        geo = http_get_json(
            f"http://api.openweathermap.org/geo/1.0/direct?q={city_q}&limit=1&appid={api_key}"
        )
        if not geo:
            raise RuntimeError(f"City not found: {city}")
        lat, lon = geo[0]["lat"], geo[0]["lon"]
        weather = http_get_json(
            f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={api_key}&units=metric"
        )
        air = http_get_json(
            f"https://api.openweathermap.org/data/2.5/air_pollution?lat={lat}&lon={lon}&appid={api_key}"
        )
        return weather, air

    def _ai_actions(self, esp: dict, weather: dict, air: dict, force_fail_safe: bool = False):
        now = time.time()
        advice = None
        pollution_comment = None
        fan_ai_speed = self.ai_applied_speed

        room_temp = esp.get("temp")
        humidity = esp.get("humidity")
        outside_temp = weather["main"]["temp"]
        outside_humidity = weather["main"]["humidity"]
        air_main = air["list"][0]["main"]["aqi"]
        comps = air["list"][0]["components"]
        profile_name = self.profile_var.get().strip().lower()
        profile = PROFILE_CONFIG.get(profile_name, PROFILE_CONFIG["aggressive"])

        if self.ai_auto_var.get():
            baseline = self._curve_baseline_speed(air_main, comps, profile)

            ai_target = baseline
            use_llm_fan = (not force_fail_safe) and (now - self.last_fan_ai_ts > 45)
            if use_llm_fan:
                prompt = (
                    "You are controlling a DIY purifier with a strong 12V industrial fan and Xiaomi filter. "
                    f"Current control profile is {profile_name}. "
                    f"Return one integer only from {profile['min_speed']} to {profile['max_speed']}. "
                    f"AQI={air_main} ({aqi_label(air_main)}), PM2.5={comps.get('pm2_5', 0):.1f}, "
                    f"PM10={comps.get('pm10', 0):.1f}, NO2={comps.get('no2', 0):.1f}, O3={comps.get('o3', 0):.1f}, "
                    f"RoomTemp={room_temp}, RoomHumidity={humidity}, OutsideTemp={outside_temp:.1f}. "
                    f"Baseline speed suggestion is {baseline}."
                )
                try:
                    raw = ollama_generate(
                        prompt,
                        model=self.model_var.get().strip(),
                        ollama_url=self.ollama_url_var.get().strip(),
                    )
                    ai_target = extract_speed(raw)
                except Exception:
                    ai_target = baseline
                    force_fail_safe = True
                self.last_fan_ai_ts = now
            elif self.ai_target_speed is not None:
                ai_target = self.ai_target_speed

            blend_ai = 0.35 if not force_fail_safe else 0.0
            blended_target = int(round((baseline * (1.0 - blend_ai)) + (ai_target * blend_ai)))
            self.ai_target_speed = int(clamp(blended_target, profile["min_speed"], profile["max_speed"]))

            current_speed = int(esp.get("speed", 0))
            if self.ai_applied_speed is None:
                self.ai_applied_speed = current_speed

            # Slew-rate limiter + deadband to avoid random on/off jumps.
            error = self.ai_target_speed - self.ai_applied_speed
            step = int(clamp(error, -profile["step"], profile["step"]))
            if abs(error) >= 2:
                self.ai_applied_speed += step
            self.ai_applied_speed = int(clamp(self.ai_applied_speed, profile["min_speed"], profile["max_speed"]))

            should_push = (
                abs(self.ai_applied_speed - current_speed) >= 2
                and (now - self.last_ai_push_ts) > 3
            )
            if should_push:
                self._ensure_esp_manual(esp)
                confirm = self._send_esp_command_json(f"/set?speed={self.ai_applied_speed}")
                self.last_esp = confirm
                self.esp_auto_mode = bool(confirm.get("auto", False))
                self.ai_applied_speed = int(confirm.get("speed", self.ai_applied_speed))
                self.last_ai_push_ts = now

            fan_ai_speed = self.ai_applied_speed

        use_llm_advice = (not force_fail_safe) and (now - self.last_advice_ts > 45)
        if use_llm_advice:
            temp_prompt = (
                "Compare room and outside weather and suggest clothing in one short sentence. "
                f"Room temp {room_temp}C, room humidity {humidity}%, outside temp {outside_temp:.1f}C, "
                f"outside humidity {outside_humidity}%, conditions {weather['weather'][0]['description']}."
            )
            try:
                advice = ollama_generate(
                    temp_prompt,
                    model=self.model_var.get().strip(),
                    ollama_url=self.ollama_url_var.get().strip(),
                )
            except Exception:
                delta = room_temp - outside_temp if isinstance(room_temp, (int, float)) else 0
                if delta > 4:
                    advice = "Outside is cooler than your room. Wear a light jacket if going out."
                elif delta < -4:
                    advice = "Outside is warmer than your room. Light, breathable clothes are best."
                else:
                    advice = "Indoor and outdoor temperatures are similar; regular comfortable clothing is fine."
            self.last_advice_ts = now
        elif advice is None:
            delta = room_temp - outside_temp if isinstance(room_temp, (int, float)) else 0
            if delta > 4:
                advice = "Outside is cooler than your room. Wear a light jacket if going out."
            elif delta < -4:
                advice = "Outside is warmer than your room. Light, breathable clothes are best."
            else:
                advice = "Indoor and outdoor temperatures are similar; regular comfortable clothing is fine."

        use_llm_pollution = (not force_fail_safe) and (now - self.last_pollution_comment_ts > 60)
        if use_llm_pollution:
            pollution_prompt = (
                "In one short sentence, explain what this outdoor air quality means for comfort/health "
                "and whether to keep purifier fan low, medium, or high. "
                f"AQI={air_main} ({aqi_label(air_main)}), PM2.5={comps.get('pm2_5', 0):.1f}, "
                f"PM10={comps.get('pm10', 0):.1f}, NO2={comps.get('no2', 0):.1f}, O3={comps.get('o3', 0):.1f}."
            )
            try:
                pollution_comment = ollama_generate(
                    pollution_prompt,
                    model=self.model_var.get().strip(),
                    ollama_url=self.ollama_url_var.get().strip(),
                )
            except Exception:
                pollution_comment = self._fallback_pollution_comment(air_main, comps)
            self.last_pollution_comment_ts = now
        elif pollution_comment is None:
            pollution_comment = self._fallback_pollution_comment(air_main, comps)

        return fan_ai_speed, advice, pollution_comment

    def _fallback_pollution_comment(self, aqi: int, comps: dict) -> str:
        pm25 = float(comps.get("pm2_5", 0.0))
        if aqi <= 2 and pm25 < 25:
            return "Air looks clean right now; low purifier speed is usually enough."
        if aqi == 3 or pm25 < 55:
            return "Air is moderate; medium fan speed helps keep indoor air fresher."
        if aqi == 4 or pm25 < 90:
            return "Air quality is poor; run medium-high to high fan speed and limit outside air intake."
        return "Air quality is very poor; keep purifier on high and reduce exposure to outdoor air."

    def _curve_baseline_speed(self, aqi: int, comps: dict, profile: dict) -> int:
        pm25 = float(comps.get("pm2_5", 0.0))
        pm10 = float(comps.get("pm10", 0.0))
        no2 = float(comps.get("no2", 0.0))
        o3 = float(comps.get("o3", 0.0))

        risk = 0.0
        risk += (max(1, min(5, int(aqi))) - 1) / 4.0 * profile["aqi_weight"]
        risk += clamp(pm25 / 55.0, 0.0, 1.0) * profile["pm25_weight"]
        risk += clamp(pm10 / 120.0, 0.0, 1.0) * profile["pm10_weight"]
        risk += clamp(no2 / 200.0, 0.0, 1.0) * 0.05
        risk += clamp(o3 / 180.0, 0.0, 1.0) * 0.05
        risk = clamp(risk, 0.0, 1.0)

        shaped = math.pow(risk, profile["shape"])
        eased = 0.5 - 0.5 * math.cos(math.pi * shaped)
        if self.calibration and isinstance(self.calibration.get("samples"), list):
            pwm = self._pwm_from_calibration(eased, profile)
            if pwm is not None:
                return int(clamp(pwm, profile["min_speed"], profile["max_speed"]))
        return int(round(profile["min_speed"] + (eased * (profile["max_speed"] - profile["min_speed"]))))

    def _pwm_from_calibration(self, demand01: float, profile: dict) -> int | None:
        samples = self.calibration.get("samples") if self.calibration else None
        if not samples:
            return None
        valid = [{"pwm": int(s["pwm"]), "rpm": int(s["rpm"])} for s in samples if "pwm" in s and "rpm" in s]
        if len(valid) < 2:
            return None
        valid.sort(key=lambda x: x["rpm"])
        spin_rpm = int(self.calibration.get("spin_up_rpm", valid[0]["rpm"]))
        max_rpm = int(clamp(int(self.calibration.get("max_rpm", valid[-1]["rpm"])), 0, FAN_APP_MAX_RPM))
        if max_rpm <= spin_rpm:
            return None

        target_rpm = int(round(spin_rpm + clamp(demand01, 0.0, 1.0) * (max_rpm - spin_rpm)))

        if target_rpm <= valid[0]["rpm"]:
            return valid[0]["pwm"]
        if target_rpm >= valid[-1]["rpm"]:
            return valid[-1]["pwm"]

        for i in range(1, len(valid)):
            lo = valid[i - 1]
            hi = valid[i]
            if lo["rpm"] <= target_rpm <= hi["rpm"]:
                if hi["rpm"] == lo["rpm"]:
                    return hi["pwm"]
                frac = (target_rpm - lo["rpm"]) / float(hi["rpm"] - lo["rpm"])
                return int(round(lo["pwm"] + frac * (hi["pwm"] - lo["pwm"])))
        return None

    def _ensure_esp_manual(self, esp: dict):
        if esp.get("auto"):
            confirm = self._send_esp_command_json("/toggle")
            self.last_esp = confirm
            self.esp_auto_mode = bool(confirm.get("auto", False))

    def _manual_speed_changed(self, value):
        try:
            speed = int(float(value))
            self.current_speed_label.configure(text=f"{speed}%")
            if self.slider_syncing:
                return
            if self.ai_auto_var.get():
                return
            now = time.time()
            if now - self.last_manual_send_ts < 0.2:
                return
            self.last_manual_send_ts = now
            threading.Thread(target=self._send_manual_speed, args=(speed,), daemon=True).start()
        except Exception:
            pass

    def _send_manual_speed(self, speed: int):
        try:
            if self.esp_auto_mode:
                confirm_toggle = self._send_esp_command_json("/toggle")
                self.last_esp = confirm_toggle
                self.esp_auto_mode = bool(confirm_toggle.get("auto", False))
            confirm = self._send_esp_command_json(f"/set?speed={speed}")
            self.last_esp = confirm
            confirmed_speed = confirm.get("speed", speed)
            seq = confirm.get("cmd_seq", "-")
            self._set_status(f"Manual speed set: {confirmed_speed}% (seq {seq})")
        except Exception as e:
            self._set_status(f"Manual set error: {e}")

    def _on_ai_mode_toggle(self):
        if self.ai_auto_var.get():
            self.ai_target_speed = None
            self.ai_applied_speed = None
            self.last_fan_ai_ts = 0.0
            self.last_ai_push_ts = 0.0
            self.fail_safe_mode = False
            self.slider.state(["disabled"])
            self._set_status("AI auto fan mode enabled")
            self.refresh_async()
            return
        self.slider.state(["!disabled"])
        threading.Thread(target=self._force_manual_mode, daemon=True).start()

    def _force_manual_mode(self):
        try:
            esp = self._read_esp()
            if esp.get("auto"):
                confirm = self._send_esp_command_json("/toggle")
                self.last_esp = confirm
                self.esp_auto_mode = bool(confirm.get("auto", False))
            else:
                self.esp_auto_mode = False
            self._set_status("AI mode off: ESP set to MANUAL")
        except Exception as e:
            self._set_status(f"Mode switch error: {e}")

    def _update_ui(self, esp: dict, weather: dict, air: dict, fan_ai_speed, advice, pollution_comment):
        outside = weather["main"]
        weather_desc = weather["weather"][0]["description"].title()
        air_info = air["list"][0]
        comps = air_info["components"]
        aqi = air_info["main"]["aqi"]

        mode_text = "AUTO" if esp.get("auto") else "MANUAL"
        self.esp_auto_mode = bool(esp.get("auto"))
        seq = esp.get("cmd_seq", "-")
        self.mode_label.configure(text=f"Mode: {mode_text} | Profile: {self.profile_var.get()} | Seq: {seq}")
        self.rpm_label.configure(text=f"Fan RPM: {esp.get('rpm', '--')}")
        current_speed = esp.get("speed", "--")
        self.current_speed_label.configure(text=f"{current_speed}%")
        self.slider_syncing = True
        self.slider.set(esp.get("speed", 0))
        self.slider_syncing = False
        if self.ai_auto_var.get():
            self.slider.state(["disabled"])
        else:
            self.slider.state(["!disabled"])
        self.aqi_label.configure(text=f"AQI: {aqi} ({aqi_label(aqi)})")
        self.pollutant_label.configure(
            text=(
                f"PM2.5 {comps.get('pm2_5', 0):.1f} | PM10 {comps.get('pm10', 0):.1f} | "
                f"NO2 {comps.get('no2', 0):.1f} | O3 {comps.get('o3', 0):.1f}"
            )
        )
        if fan_ai_speed is not None and self.ai_auto_var.get():
            target_txt = self.ai_target_speed if self.ai_target_speed is not None else fan_ai_speed
            fs = "fail-safe" if self.fail_safe_mode else "ai"
            self.ai_fan_label.configure(text=f"AI fan decision: {fan_ai_speed}% (target {target_txt}%, {fs})")
            self.speed_detail_label.configure(text=f"Target: {target_txt}% | Source: AI ({fs})")
        elif not self.ai_auto_var.get():
            self.ai_fan_label.configure(text="AI fan decision: off")
            self.speed_detail_label.configure(text="Target: manual slider | Source: manual")
        if pollution_comment:
            self.ai_air_label.configure(text=f"Air insight: {pollution_comment}")

        if self.calibration and isinstance(self.calibration.get("samples"), list):
            spin = self.calibration.get("spin_up_pwm", "--")
            mx = self.calibration.get("max_rpm", "--")
            self.calibration_label.configure(text=f"Calibration: tuned (spin-up {spin}%, max {mx} RPM)")
        else:
            self.calibration_label.configure(text="Calibration: not tuned")

        self.room_temp_label.configure(text=f"{esp.get('temp', '--')} C")
        self.room_hum_label.configure(text=f"Humidity: {esp.get('humidity', '--')} %")
        self.out_temp_label.configure(text=f"{outside.get('temp', '--')} C")
        self.out_hum_label.configure(text=f"Humidity: {outside.get('humidity', '--')} %")
        self.out_desc_label.configure(text=f"Conditions: {weather_desc}")
        if advice:
            self.ai_advice_label.configure(text=f"AI advice: {advice}")


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
