import json
import math
import os
import re
import threading
import time
import tkinter as tk
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


# DIY purifier profile: Noctua industrial fan + Xiaomi filter.
AI_MIN_SPEED = 38
AI_MAX_SPEED = 100


def load_settings() -> dict:
    defaults = {
        "city": DEFAULT_CITY,
        "esp_base_url": DEFAULT_ESP_BASE_URL,
        "openweather_api_key": DEFAULT_OPENWEATHER_API_KEY,
        "ollama_url": DEFAULT_OLLAMA_URL,
        "ollama_model": DEFAULT_OLLAMA_MODEL,
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


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Smart Air Purifier Desktop")
        self.root.geometry("920x520")
        self.root.configure(bg="#0b132b")

        settings = load_settings()
        self.city_var = tk.StringVar(value=settings["city"])
        self.esp_url_var = tk.StringVar(value=settings["esp_base_url"])
        self.api_key_var = tk.StringVar(value=settings["openweather_api_key"])
        self.ollama_url_var = tk.StringVar(value=settings["ollama_url"])
        self.model_var = tk.StringVar(value=settings["ollama_model"])
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

        self._build_ui()
        self._schedule_refresh(initial=True)

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
        ttk.Button(header, text="Refresh", command=self.refresh_async).pack(side="left", padx=8)
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
        self.speed_label = ttk.Label(self.fan_card, text="Fan Speed: --%", style="Big.TLabel")
        self.speed_label.pack(anchor="w", pady=(4, 10))
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
        self.slider = ttk.Scale(self.fan_card, from_=0, to=100, orient="horizontal", command=self._manual_speed_changed)
        self.slider.pack(fill="x", pady=6)
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

        def save_and_close():
            self.persist_settings()
            self.status_var.set("Settings saved")
            win.destroy()

        buttons = ttk.Frame(frame, style="Card.TFrame")
        buttons.grid(row=len(fields), column=0, columnspan=2, sticky="e", pady=(14, 0))
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
            }
        )

    def _schedule_refresh(self, initial: bool = False):
        if initial:
            self.refresh_async()
        self.root.after(12000, self._schedule_refresh)

    def refresh_async(self):
        t = threading.Thread(target=self._refresh_worker, daemon=True)
        t.start()

    def _refresh_worker(self):
        try:
            esp = self._read_esp()
            self.root.after(0, lambda: self._set_esp_indicator(True))
        except Exception as e:
            self.root.after(0, lambda: self._set_esp_indicator(False))
            self.status_var.set(f"ESP connection error: {e}")
            return

        try:
            weather, air = self._read_openweather(self.city_var.get().strip())
            fan_ai_speed, advice, pollution_comment = self._ai_actions(esp, weather, air)
            self.root.after(0, lambda: self._update_ui(esp, weather, air, fan_ai_speed, advice, pollution_comment))
            self.status_var.set(f"Updated at {time.strftime('%H:%M:%S')}")
        except Exception as e:
            self.status_var.set(f"Update error: {e}")

    def _set_esp_indicator(self, online: bool):
        if online:
            self.esp_conn_label.configure(text="ESP32: Online", bg="#14532d", fg="#dcfce7")
        else:
            self.esp_conn_label.configure(text="ESP32: Offline", bg="#7f1d1d", fg="#fecaca")

    def _esp_base_url(self):
        return self.esp_url_var.get().strip().rstrip("/")

    def _read_esp(self):
        return http_get_json(f"{self._esp_base_url()}/data")

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

    def _ai_actions(self, esp: dict, weather: dict, air: dict):
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

        if self.ai_auto_var.get():
            baseline = self._curve_baseline_speed(air_main, comps)

            ai_target = baseline
            if now - self.last_fan_ai_ts > 45:
                prompt = (
                    "You are controlling a DIY purifier with a strong 12V industrial fan and Xiaomi filter. "
                    "Be aggressive for pollution spikes. Return one integer only from 38 to 100. "
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
                self.last_fan_ai_ts = now
            elif self.ai_target_speed is not None:
                ai_target = self.ai_target_speed

            # Blend AI result with deterministic curve so behavior stays stable.
            blended_target = int(round((baseline * 0.65) + (ai_target * 0.35)))
            self.ai_target_speed = int(clamp(blended_target, AI_MIN_SPEED, AI_MAX_SPEED))

            current_speed = int(esp.get("speed", 0))
            if self.ai_applied_speed is None:
                self.ai_applied_speed = current_speed

            # Slew-rate limiter + deadband to avoid random on/off jumps.
            error = self.ai_target_speed - self.ai_applied_speed
            step = int(clamp(error, -12, 12))
            if abs(error) >= 2:
                self.ai_applied_speed += step
            self.ai_applied_speed = int(clamp(self.ai_applied_speed, AI_MIN_SPEED, AI_MAX_SPEED))

            should_push = (
                abs(self.ai_applied_speed - current_speed) >= 2
                and (now - self.last_ai_push_ts) > 3
            )
            if should_push:
                self._ensure_esp_manual(esp)
                http_get_text(f"{self._esp_base_url()}/set?speed={self.ai_applied_speed}")
                self.last_ai_push_ts = now

            fan_ai_speed = self.ai_applied_speed

        if now - self.last_advice_ts > 45:
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

        if now - self.last_pollution_comment_ts > 60:
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

    def _curve_baseline_speed(self, aqi: int, comps: dict) -> int:
        pm25 = float(comps.get("pm2_5", 0.0))
        pm10 = float(comps.get("pm10", 0.0))
        no2 = float(comps.get("no2", 0.0))
        o3 = float(comps.get("o3", 0.0))

        risk = 0.0
        risk += (max(1, min(5, int(aqi))) - 1) / 4.0 * 0.50
        risk += clamp(pm25 / 55.0, 0.0, 1.0) * 0.32
        risk += clamp(pm10 / 120.0, 0.0, 1.0) * 0.10
        risk += clamp(no2 / 200.0, 0.0, 1.0) * 0.05
        risk += clamp(o3 / 180.0, 0.0, 1.0) * 0.05
        risk = clamp(risk, 0.0, 1.0)

        # Aggressive S-curve: faster rise at moderate pollution.
        shaped = math.pow(risk, 0.75)
        eased = 0.5 - 0.5 * math.cos(math.pi * shaped)
        return int(round(AI_MIN_SPEED + (eased * (AI_MAX_SPEED - AI_MIN_SPEED))))

    def _ensure_esp_manual(self, esp: dict):
        if esp.get("auto"):
            http_get_text(f"{self._esp_base_url()}/toggle")
            self.esp_auto_mode = False

    def _manual_speed_changed(self, value):
        try:
            speed = int(float(value))
            self.speed_label.configure(text=f"Fan Speed: {speed}%")
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
                http_get_text(f"{self._esp_base_url()}/toggle")
                self.esp_auto_mode = False
            http_get_text(f"{self._esp_base_url()}/set?speed={speed}")
            self.status_var.set(f"Manual speed set: {speed}%")
        except Exception as e:
            self.status_var.set(f"Manual set error: {e}")

    def _on_ai_mode_toggle(self):
        if self.ai_auto_var.get():
            self.ai_target_speed = None
            self.ai_applied_speed = None
            self.last_fan_ai_ts = 0.0
            self.last_ai_push_ts = 0.0
            self.status_var.set("AI auto fan mode enabled")
            self.refresh_async()
            return
        threading.Thread(target=self._force_manual_mode, daemon=True).start()

    def _force_manual_mode(self):
        try:
            esp = self._read_esp()
            if esp.get("auto"):
                http_get_text(f"{self._esp_base_url()}/toggle")
            self.esp_auto_mode = False
            self.status_var.set("AI mode off: ESP set to MANUAL")
        except Exception as e:
            self.status_var.set(f"Mode switch error: {e}")

    def _update_ui(self, esp: dict, weather: dict, air: dict, fan_ai_speed, advice, pollution_comment):
        outside = weather["main"]
        weather_desc = weather["weather"][0]["description"].title()
        air_info = air["list"][0]
        comps = air_info["components"]
        aqi = air_info["main"]["aqi"]

        mode_text = "AUTO" if esp.get("auto") else "MANUAL"
        self.esp_auto_mode = bool(esp.get("auto"))
        self.mode_label.configure(text=f"Mode: {mode_text}")
        self.rpm_label.configure(text=f"Fan RPM: {esp.get('rpm', '--')}")
        self.speed_label.configure(text=f"Fan Speed: {esp.get('speed', '--')}%")
        self.slider_syncing = True
        self.slider.set(esp.get("speed", 0))
        self.slider_syncing = False
        self.aqi_label.configure(text=f"AQI: {aqi} ({aqi_label(aqi)})")
        self.pollutant_label.configure(
            text=(
                f"PM2.5 {comps.get('pm2_5', 0):.1f} | PM10 {comps.get('pm10', 0):.1f} | "
                f"NO2 {comps.get('no2', 0):.1f} | O3 {comps.get('o3', 0):.1f}"
            )
        )
        if fan_ai_speed is not None and self.ai_auto_var.get():
            target_txt = self.ai_target_speed if self.ai_target_speed is not None else fan_ai_speed
            self.ai_fan_label.configure(text=f"AI fan decision: {fan_ai_speed}% (target {target_txt}%)")
        elif not self.ai_auto_var.get():
            self.ai_fan_label.configure(text="AI fan decision: off")
        if pollution_comment:
            self.ai_air_label.configure(text=f"Air insight: {pollution_comment}")

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
