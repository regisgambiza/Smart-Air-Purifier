#include <WiFi.h>
#include <WebServer.h>
#include <ArduinoOTA.h>
#include <math.h>
#include <Wire.h>
#include <Adafruit_SHT31.h>
#include <OneWire.h>
#include <DallasTemperature.h>

// ===== WIFI =====
const char* ssid = "Nishcha_2.4G";
const char* password = "0646362455N";
const char* otaHostname = "smart-air-purifier";
const char* otaPassword = "1234";

// ===== FAN CONFIG =====
#define FAN_COUNT 1

const int pwmPins[FAN_COUNT]  = {18};
const int tachPins[FAN_COUNT] = {34};

#define PWM_FREQ 25000
#define PWM_RES 8
#define FAN_TACH_PULSES_PER_REV 2
#define FAN_MAX_VALID_RPM 2200
#define TACH_DEBOUNCE_US 1800

// ===== TEMP SENSOR =====
#define ONE_WIRE_BUS 4
#define I2C_SDA_PIN 21
#define I2C_SCL_PIN 22

OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature sensors(&oneWire);
Adafruit_SHT31 sht30 = Adafruit_SHT31();

// ===== GLOBALS =====
WebServer server(80);

volatile uint32_t tachCount[FAN_COUNT] = {0};
volatile uint32_t tachLastMicros[FAN_COUNT] = {0};
uint32_t rpm[FAN_COUNT] = {0};
float rpmFiltered[FAN_COUNT] = {0.0f};
uint8_t fanSpeed[FAN_COUNT] = {40};

enum ControlMode : uint8_t {
    CONTROL_MODE_MANUAL = 0,
    CONTROL_MODE_CLASSIC_AUTO = 1,
    CONTROL_MODE_AI_ASSIST = 2,
};

struct FanProfile {
    const char* key;
    uint8_t minSpeed;
    uint8_t maxSpeed;
    float aqiWeight;
    float pm25Weight;
    float pm10Weight;
    float shape;
    uint8_t step;
};

const FanProfile PROFILE_CONFIG[] = {
  {"sleep", 20, 60, 0.40f, 0.40f, 0.10f, 0.98f, 6},
  {"quiet", 40, 90, 0.46f, 0.34f, 0.12f, 0.95f, 10},
  {"balanced", 50, 96, 0.54f, 0.34f, 0.10f, 0.75f, 14},
  {"allergen", 55, 98, 0.65f, 0.55f, 0.05f, 0.50f, 20},
  {"pet", 50, 98, 0.52f, 0.50f, 0.12f, 0.65f, 16},
  {"turbo", 90, 100, 0.75f, 0.60f, 0.15f, 0.45f, 30},
  {"eco", 35, 88, 0.40f, 0.30f, 0.10f, 1.25f, 8},
  {"auto", 45, 100, 0.58f, 0.38f, 0.09f, 0.70f, 14},
  {"aggressive", 60, 100, 0.60f, 0.34f, 0.10f, 0.60f, 18},
};
const size_t PROFILE_COUNT = sizeof(PROFILE_CONFIG) / sizeof(PROFILE_CONFIG[0]);

ControlMode controlMode = CONTROL_MODE_CLASSIC_AUTO;
size_t controlProfileIndex = 2;
uint8_t autoAppliedSpeed = 60;

float dsTemperatureC = 0;
float shtTemperatureC = NAN;
float humidityRH = NAN;
bool shtOnline = false;
uint32_t lastCommandMs = 0;
uint32_t commandSeq = 0;
String lastCommand = "boot";

const FanProfile& activeProfile() {
    if (controlProfileIndex >= PROFILE_COUNT) {
        controlProfileIndex = PROFILE_COUNT - 1;
    }
    return PROFILE_CONFIG[controlProfileIndex];
}

bool modeIsAutomatic(ControlMode mode) {
    return mode != CONTROL_MODE_MANUAL;
}

const char* controlModeKey(ControlMode mode) {
    switch (mode) {
        case CONTROL_MODE_MANUAL: return "manual";
        case CONTROL_MODE_CLASSIC_AUTO: return "classic_auto";
        case CONTROL_MODE_AI_ASSIST: return "ai_assist";
        default: return "classic_auto";
    }
}

const char* controlModeLabel(ControlMode mode) {
    switch (mode) {
        case CONTROL_MODE_MANUAL: return "Manual";
        case CONTROL_MODE_CLASSIC_AUTO: return "Classic Auto";
        case CONTROL_MODE_AI_ASSIST: return "AI Assist";
        default: return "Classic Auto";
    }
}

ControlMode parseControlMode(const String& raw) {
    String value = raw;
    value.trim();
    value.toLowerCase();
    if (value == "manual") return CONTROL_MODE_MANUAL;
    if (value == "ai_assist") return CONTROL_MODE_AI_ASSIST;
    return CONTROL_MODE_CLASSIC_AUTO;
}

size_t parseControlProfileIndex(const String& raw) {
    String value = raw;
    value.trim();
    value.toLowerCase();
    for (size_t i = 0; i < PROFILE_COUNT; i++) {
        if (value == PROFILE_CONFIG[i].key) {
            return i;
        }
    }
    return controlProfileIndex;
}

// ===== INTERRUPTS =====
void IRAM_ATTR tach0() {
    uint32_t nowUs = micros();
    if ((uint32_t)(nowUs - tachLastMicros[0]) >= TACH_DEBOUNCE_US) {
        tachCount[0]++;
        tachLastMicros[0] = nowUs;
    }
}

// ===== SET FAN SPEED =====
void setFanSpeed(int fan, uint8_t percent) {
    percent = constrain(percent, 0, 100);
    fanSpeed[fan] = percent;
    uint32_t duty = map(percent, 0, 100, 0, 255);
    ledcWrite(fan, duty);
}

void syncAutoAppliedSpeedToCurrent() {
    const FanProfile& profile = activeProfile();
    autoAppliedSpeed = (uint8_t)constrain((int)fanSpeed[0], (int)profile.minSpeed, (int)profile.maxSpeed);
}

// ===== PROFILE-BASED FAN CURVE =====
uint8_t calculateAutoTargetSpeed(float roomTemp, float roomHumidity, ControlMode mode, const FanProfile& profile) {
    float safeTemp = isnan(roomTemp) ? 27.0f : roomTemp;
    float tempRisk = constrain((safeTemp - 24.0f) / 10.0f, 0.0f, 1.0f);

    float humidityRisk = 0.35f;
    if (!isnan(roomHumidity)) {
        humidityRisk = constrain((roomHumidity - 45.0f) / 30.0f, 0.0f, 1.0f);
    }

    float risk = constrain((tempRisk * 0.75f) + (humidityRisk * 0.25f), 0.0f, 1.0f);
    if (mode == CONTROL_MODE_AI_ASSIST) {
        risk = constrain(risk + 0.08f, 0.0f, 1.0f);
    }

    float shaped = powf(risk, profile.shape);
    int target = (int)roundf((float)profile.minSpeed + (shaped * (float)(profile.maxSpeed - profile.minSpeed)));
    if (mode == CONTROL_MODE_AI_ASSIST) {
        target += 4;
    }
    target = constrain(target, (int)profile.minSpeed, (int)profile.maxSpeed);
    return (uint8_t)target;
}

uint8_t applyAutoSlew(uint8_t target, const FanProfile& profile) {
    int error = (int)target - (int)autoAppliedSpeed;
    int step = constrain(error, -(int)profile.step, (int)profile.step);
    if (abs(error) >= 2) {
        autoAppliedSpeed = (uint8_t)constrain((int)autoAppliedSpeed + step, (int)profile.minSpeed, (int)profile.maxSpeed);
    } else {
        autoAppliedSpeed = target;
    }
    return autoAppliedSpeed;
}

// ===== JSON STATUS =====
String jsonTemperatureOrNull(float value) {
    if (isnan(value) || isinf(value) || value < -55.0f || value > 130.0f) {
        return "null";
    }
    return String(value, 1);
}

String jsonHumidityOrNull(float value) {
    if (isnan(value) || isinf(value) || value < 0.0f || value > 100.0f) {
        return "null";
    }
    return String(value, 1);
}

String getJSON() {
    bool autoMode = modeIsAutomatic(controlMode);
    const FanProfile& profile = activeProfile();
    uint32_t uptimeMs = millis();
    uint32_t cmdAgeMs = commandSeq > 0 ? (uptimeMs - lastCommandMs) : 0;
    String json = "{";
    json += "\"temp\":" + jsonTemperatureOrNull(shtTemperatureC) + ",";
    json += "\"humidity\":" + jsonHumidityOrNull(humidityRH) + ",";
    json += "\"ds_temp\":" + jsonTemperatureOrNull(dsTemperatureC) + ",";
    json += "\"sht_ok\":" + String(shtOnline ? "true" : "false") + ",";
    json += "\"auto\":" + String(autoMode ? "true" : "false") + ",";
    json += "\"control_mode\":\"" + String(controlModeKey(controlMode)) + "\",";
    json += "\"control_mode_label\":\"" + String(controlModeLabel(controlMode)) + "\",";
    json += "\"control_profile\":\"" + String(profile.key) + "\",";
    json += "\"rpm\":" + String(rpm[0]) + ",";
    json += "\"speed\":" + String(fanSpeed[0]) + ",";
    json += "\"last_cmd_ms\":" + String(lastCommandMs) + ",";
    json += "\"uptime_ms\":" + String(uptimeMs) + ",";
    json += "\"cmd_age_ms\":" + String(cmdAgeMs) + ",";
    json += "\"cmd_seq\":" + String(commandSeq) + ",";
    json += "\"last_cmd\":\"" + lastCommand + "\"";
    json += "}";
    return json;
}

// ===== WEB PAGE =====
const char webpage[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Smart Air Purifier Studio</title>
<style>
:root {
  --ink: #eef2f8;
  --muted: #adbcda;
  --panel: rgba(11, 19, 44, 0.58);
  --panel-edge: rgba(171, 195, 255, 0.23);
  --accent-1: #47d7ff;
  --accent-2: #76ffb8;
  --accent-3: #ffb36a;
  --danger: #ff7d91;
  --ok: #72f5ae;
  --manual: #80d8ff;
  --classic: #ffc178;
  --ai: #6dffbf;
  --radius-lg: 24px;
  --radius-md: 16px;
  --shadow: 0 20px 60px rgba(3, 8, 25, 0.45);
}
*,
*::before,
*::after {
  box-sizing: border-box;
}
body {
  margin: 0;
  min-height: 100vh;
  font-family: "Avenir Next", "Sora", "Trebuchet MS", sans-serif;
  color: var(--ink);
  background-color: #050b1f;
  background:
    radial-gradient(1200px 650px at -8% -16%, rgba(48, 115, 240, 0.45), transparent 65%),
    radial-gradient(900px 460px at 110% -12%, rgba(31, 187, 147, 0.36), transparent 68%),
    radial-gradient(860px 580px at 50% 118%, rgba(117, 78, 201, 0.28), transparent 72%),
    linear-gradient(154deg, #060c22 0%, #0c1431 46%, #0a122a 100%);
  overflow-x: hidden;
}
body::before,
body::after {
  content: "";
  position: fixed;
  pointer-events: none;
  border-radius: 999px;
  filter: blur(80px);
  opacity: 0.45;
}
body::before {
  width: 270px;
  height: 270px;
  top: 8vh;
  right: -120px;
  background: #3c97ff;
  animation: floatA 12s ease-in-out infinite;
}
body::after {
  width: 290px;
  height: 290px;
  bottom: -120px;
  left: -110px;
  background: #5ee9b2;
  animation: floatB 14s ease-in-out infinite;
}
@keyframes floatA {
  0%, 100% { transform: translateY(0px); }
  50% { transform: translateY(18px); }
}
@keyframes floatB {
  0%, 100% { transform: translateY(0px); }
  50% { transform: translateY(-20px); }
}
@keyframes fadeUp {
  from {
    opacity: 0;
    transform: translateY(14px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
.shell {
  width: min(1100px, 94vw);
  margin: 18px auto 24px;
  display: grid;
  gap: 14px;
}
.card {
  background: var(--panel);
  border: 1px solid var(--panel-edge);
  box-shadow: var(--shadow);
  border-radius: var(--radius-lg);
  backdrop-filter: blur(10px);
  animation: fadeUp 400ms ease-out both;
}
.hero {
  padding: 18px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.kicker {
  letter-spacing: 0.3em;
  text-transform: uppercase;
  font-size: 0.73rem;
  color: var(--muted);
}
h1 {
  margin: 4px 0 6px;
  font-size: clamp(1.35rem, 2.2vw, 2.15rem);
  font-weight: 700;
  line-height: 1.06;
}
.subline {
  color: var(--muted);
  font-size: 0.98rem;
}
.hero-right {
  display: grid;
  gap: 10px;
  justify-items: end;
}
.pill {
  padding: 7px 12px;
  border-radius: 999px;
  font-weight: 700;
  font-size: 0.78rem;
  letter-spacing: 0.04em;
  border: 1px solid transparent;
}
.pill.online {
  color: #d7ffe8;
  background: rgba(35, 151, 84, 0.24);
  border-color: rgba(105, 244, 170, 0.5);
}
.pill.offline {
  color: #ffe1e8;
  background: rgba(181, 44, 82, 0.27);
  border-color: rgba(252, 125, 163, 0.45);
}
.refresh {
  color: var(--muted);
  font-size: 0.84rem;
}
.layout {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 0.9fr);
  gap: 14px;
}
.control-panel,
.climate-panel,
.ops-panel {
  padding: 18px;
}
.section-label {
  color: var(--muted);
  font-size: 0.74rem;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}
.mode-line {
  margin-top: 7px;
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
}
.mode-title {
  font-size: 1.25rem;
  font-weight: 700;
}
.badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 5px 10px;
  border-radius: 999px;
  font-size: 0.73rem;
  font-weight: 700;
  border: 1px solid transparent;
}
.badge.manual {
  background: rgba(88, 163, 255, 0.2);
  border-color: rgba(128, 216, 255, 0.54);
  color: #d8f4ff;
}
.badge.classic {
  background: rgba(248, 146, 63, 0.2);
  border-color: rgba(255, 196, 122, 0.53);
  color: #ffe9ce;
}
.badge.ai {
  background: rgba(54, 180, 118, 0.2);
  border-color: rgba(116, 255, 191, 0.53);
  color: #e1ffef;
}
.orb-wrap {
  margin: 16px 0 10px;
  display: flex;
  justify-content: center;
}
.fan-orb {
  --fill: 40%;
  width: min(190px, 60vw);
  aspect-ratio: 1;
  border-radius: 50%;
  display: grid;
  place-items: center;
  text-align: center;
  color: #f5f9ff;
  border: 1px solid rgba(150, 184, 255, 0.4);
  background:
    radial-gradient(circle at 35% 30%, rgba(255, 255, 255, 0.22), rgba(255, 255, 255, 0.03) 45%, rgba(3, 12, 31, 0.68) 70%),
    conic-gradient(from 230deg, #54d4ff 0 var(--fill), rgba(255, 255, 255, 0.15) var(--fill) 100%);
  box-shadow:
    inset 0 -16px 26px rgba(4, 9, 25, 0.56),
    0 0 0 12px rgba(81, 142, 255, 0.1),
    0 20px 40px rgba(3, 9, 24, 0.5);
}
.fan-orb .num {
  font-size: 2.1rem;
  font-weight: 800;
  line-height: 1;
}
.fan-orb .unit {
  font-size: 0.82rem;
  letter-spacing: 0.09em;
  color: var(--muted);
}
.quick-mode {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 12px;
}
.mini-btn {
  border: 1px solid rgba(180, 206, 255, 0.35);
  background: rgba(255, 255, 255, 0.06);
  color: var(--ink);
  border-radius: 11px;
  padding: 8px 6px;
  font-size: 0.78rem;
  font-weight: 700;
  cursor: pointer;
  transition: background 140ms ease, transform 140ms ease, border-color 140ms ease;
}
.mini-btn:active {
  transform: translateY(1px);
}
.mini-btn.active {
  border-color: rgba(120, 243, 198, 0.55);
  background: rgba(72, 198, 150, 0.18);
}
.fields {
  display: grid;
  gap: 10px;
}
.field {
  display: grid;
  gap: 6px;
  font-size: 0.83rem;
  color: var(--muted);
}
select,
button.action {
  width: 100%;
  border-radius: 12px;
  border: 1px solid rgba(180, 206, 255, 0.35);
  background: rgba(255, 255, 255, 0.08);
  color: var(--ink);
  padding: 10px 11px;
  font-size: 0.93rem;
  font-weight: 700;
}
select:focus,
button:focus,
input[type="range"]:focus {
  outline: 2px solid rgba(120, 213, 255, 0.5);
  outline-offset: 1px;
}
.slider-wrap {
  margin-top: 12px;
}
input[type="range"] {
  width: 100%;
  appearance: none;
  background: transparent;
}
input[type="range"]::-webkit-slider-runnable-track {
  height: 10px;
  border-radius: 999px;
  background: linear-gradient(90deg, rgba(70, 153, 255, 0.6), rgba(73, 232, 191, 0.72));
}
input[type="range"]::-webkit-slider-thumb {
  appearance: none;
  margin-top: -4px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: 2px solid #e7f5ff;
  background: #0d1532;
}
input[type="range"]::-moz-range-track {
  height: 10px;
  border-radius: 999px;
  background: linear-gradient(90deg, rgba(70, 153, 255, 0.6), rgba(73, 232, 191, 0.72));
}
input[type="range"]::-moz-range-thumb {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: 2px solid #e7f5ff;
  background: #0d1532;
}
.scale {
  margin-top: 4px;
  display: flex;
  justify-content: space-between;
  font-size: 0.75rem;
  color: var(--muted);
}
.hint {
  margin-top: 10px;
  color: var(--muted);
  font-size: 0.86rem;
}
.metric-grid {
  margin-top: 14px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.metric {
  border: 1px solid rgba(174, 196, 245, 0.25);
  background: rgba(255, 255, 255, 0.06);
  border-radius: var(--radius-md);
  padding: 12px;
}
.metric .cap {
  display: block;
  font-size: 0.75rem;
  color: var(--muted);
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.metric .main {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin-top: 6px;
}
.metric .value {
  font-size: 1.62rem;
  font-weight: 800;
}
.metric .unit {
  font-size: 0.84rem;
  color: var(--muted);
}
.sensor-note {
  margin-top: 12px;
  color: var(--muted);
  font-size: 0.88rem;
}
.ops-grid {
  margin-top: 13px;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}
.ops-item {
  border: 1px solid rgba(174, 196, 245, 0.24);
  background: rgba(255, 255, 255, 0.05);
  border-radius: 14px;
  padding: 10px;
}
.ops-item .cap {
  color: var(--muted);
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}
.ops-item .v {
  margin-top: 7px;
  font-size: 1.25rem;
  font-weight: 800;
}
.ops-footer {
  margin-top: 12px;
  display: grid;
  gap: 8px;
  color: var(--muted);
  font-size: 0.86rem;
}
.action {
  margin-top: 12px;
  cursor: pointer;
}
.action:hover {
  background: rgba(255, 255, 255, 0.14);
}
.status-note {
  margin-top: 8px;
  font-size: 0.87rem;
  color: var(--muted);
}
@media (max-width: 900px) {
  .layout {
    grid-template-columns: 1fr;
  }
  .hero-right {
    justify-items: start;
  }
  .ops-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 520px) {
  .shell {
    width: 95vw;
  }
  .hero,
  .control-panel,
  .climate-panel,
  .ops-panel {
    padding: 15px;
  }
  .metric-grid,
  .ops-grid {
    grid-template-columns: 1fr;
  }
}
</style>
</head>
<body>
<main class="shell">
  <header class="hero card">
    <div>
      <div class="kicker">Smart Air Purifier</div>
      <h1>Control Studio</h1>
      <div id="comfortLine" class="subline">Comfort score: --</div>
    </div>
    <div class="hero-right">
      <div id="linkPill" class="pill offline">Device Offline</div>
      <div class="refresh">Last update: <span id="refreshAge">--</span></div>
    </div>
  </header>

  <section class="layout">
    <article class="card control-panel">
      <div class="section-label">Control Core</div>
      <div class="mode-line">
        <div id="modeTitle" class="mode-title">--</div>
        <span id="modeBadge" class="badge classic">--</span>
      </div>

      <div class="orb-wrap">
        <div id="fanOrb" class="fan-orb">
          <div>
            <div id="speedValue" class="num">--</div>
            <div class="unit">% Fan Power</div>
          </div>
        </div>
      </div>

      <div class="quick-mode">
        <button id="manualBtn" class="mini-btn" type="button">Manual</button>
        <button id="classicBtn" class="mini-btn" type="button">Classic</button>
        <button id="aiBtn" class="mini-btn" type="button">AI Assist</button>
      </div>

      <div class="fields">
        <label class="field">
          Control mode
          <select id="modeSelect">
            <option value="manual">Manual</option>
            <option value="classic_auto">Classic Auto</option>
            <option value="ai_assist">AI Assist</option>
          </select>
        </label>
        <label class="field">
          Control profile
          <select id="profileSelect">
            <option value="quiet">Quiet</option>
            <option value="balanced">Balanced</option>
            <option value="aggressive">Aggressive</option>
          </select>
        </label>
      </div>

      <div class="slider-wrap">
        <input id="speedSlider" type="range" min="0" max="100" value="40">
        <div class="scale"><span>0%</span><span>100%</span></div>
      </div>
      <div id="controlHint" class="hint">Manual control is active.</div>
    </article>

    <article class="card climate-panel">
      <div class="section-label">Climate Mirror</div>
      <div class="metric-grid">
        <div class="metric">
          <span class="cap">Room Temp</span>
          <div class="main">
            <span id="temp" class="value">--</span>
            <span class="unit">C</span>
          </div>
        </div>
        <div class="metric">
          <span class="cap">Humidity</span>
          <div class="main">
            <span id="humidity" class="value">--</span>
            <span class="unit">%</span>
          </div>
        </div>
        <div class="metric">
          <span class="cap">DS18B20</span>
          <div class="main">
            <span id="dsTemp" class="value">--</span>
            <span class="unit">C</span>
          </div>
        </div>
        <div class="metric">
          <span class="cap">RPM</span>
          <div class="main">
            <span id="rpmValue" class="value">--</span>
            <span class="unit">fan</span>
          </div>
        </div>
      </div>
      <div id="shtStatus" class="sensor-note">I2C sensor status: --</div>
    </article>

    <article class="card ops-panel">
      <div class="section-label">Command Telemetry</div>
      <div class="ops-grid">
        <div class="ops-item">
          <div class="cap">Profile</div>
          <div id="profileTag" class="v">--</div>
        </div>
        <div class="ops-item">
          <div class="cap">Command Seq</div>
          <div id="cmdSeq" class="v">--</div>
        </div>
        <div class="ops-item">
          <div class="cap">Last Cmd</div>
          <div id="cmdName" class="v">--</div>
        </div>
      </div>
      <div class="ops-footer">
        <div>Last command age: <span id="cmdAge">--</span></div>
        <div id="statusNote" class="status-note">Waiting for the purifier...</div>
      </div>
      <button id="toggleBtn" class="action" type="button">Toggle Manual / Classic</button>
    </article>
  </section>
</main>
<script>
const dom = {
  modeTitle: document.getElementById("modeTitle"),
  modeBadge: document.getElementById("modeBadge"),
  modeSelect: document.getElementById("modeSelect"),
  profileSelect: document.getElementById("profileSelect"),
  speedSlider: document.getElementById("speedSlider"),
  speedValue: document.getElementById("speedValue"),
  fanOrb: document.getElementById("fanOrb"),
  controlHint: document.getElementById("controlHint"),
  temp: document.getElementById("temp"),
  humidity: document.getElementById("humidity"),
  dsTemp: document.getElementById("dsTemp"),
  rpmValue: document.getElementById("rpmValue"),
  shtStatus: document.getElementById("shtStatus"),
  profileTag: document.getElementById("profileTag"),
  cmdSeq: document.getElementById("cmdSeq"),
  cmdName: document.getElementById("cmdName"),
  cmdAge: document.getElementById("cmdAge"),
  comfortLine: document.getElementById("comfortLine"),
  linkPill: document.getElementById("linkPill"),
  refreshAge: document.getElementById("refreshAge"),
  statusNote: document.getElementById("statusNote"),
  manualBtn: document.getElementById("manualBtn"),
  classicBtn: document.getElementById("classicBtn"),
  aiBtn: document.getElementById("aiBtn"),
  toggleBtn: document.getElementById("toggleBtn"),
};

let latestMode = "classic_auto";
let lastFetchTs = 0;
let sliderDebounce = 0;
let sliderPointerActive = false;
let requestRunning = false;

function toNum(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function oneDecimal(value) {
  return value === null ? "--" : value.toFixed(1);
}

function whole(value) {
  return value === null ? "--" : String(Math.round(value));
}

function modeMeta(mode) {
  if (mode === "manual") {
    return {
      title: "Manual Control",
      badge: "Hands-On",
      className: "badge manual",
      hint: "Slider is active. Commands stream directly to the fan.",
    };
  }
  if (mode === "ai_assist") {
    return {
      title: "AI Assist Curve",
      badge: "Adaptive",
      className: "badge ai",
      hint: "Firmware is auto-adjusting speed with AI Assist curve.",
    };
  }
  return {
    title: "Classic Auto Curve",
    badge: "Stable",
    className: "badge classic",
    hint: "Firmware is auto-adjusting speed with Classic Auto curve.",
  };
}

function setConnectionState(online) {
  dom.linkPill.className = online ? "pill online" : "pill offline";
  dom.linkPill.textContent = online ? "Device Online" : "Device Offline";
}

function updateFanOrb(speed) {
  const clamped = Math.max(0, Math.min(100, Number(speed) || 0));
  dom.fanOrb.style.setProperty("--fill", clamped + "%");
}

function comfortScore(temp, humidity) {
  if (temp === null || humidity === null) return null;
  const tempPenalty = Math.abs(temp - 23.0) * 4.5;
  const humidityPenalty = Math.abs(humidity - 50.0) * 1.4;
  const raw = 100 - tempPenalty - humidityPenalty;
  return Math.max(0, Math.min(100, Math.round(raw)));
}

function setActiveQuickMode(mode) {
  dom.manualBtn.classList.toggle("active", mode === "manual");
  dom.classicBtn.classList.toggle("active", mode === "classic_auto");
  dom.aiBtn.classList.toggle("active", mode === "ai_assist");
}

function renderState(data) {
  const mode = String(data.control_mode || (data.auto ? "classic_auto" : "manual")).toLowerCase();
  latestMode = mode;
  const profile = String(data.control_profile || "aggressive").toLowerCase();
  const speed = toNum(data.speed);
  const rpm = toNum(data.rpm);
  const temp = toNum(data.temp);
  const humidity = toNum(data.humidity);
  const dsTemp = toNum(data.ds_temp);
  const commandAgeMs = toNum(data.cmd_age_ms);
  const meta = modeMeta(mode);
  const comfort = comfortScore(temp, humidity);

  dom.modeTitle.textContent = meta.title;
  dom.modeBadge.className = meta.className;
  dom.modeBadge.textContent = meta.badge;
  dom.controlHint.textContent = meta.hint;
  dom.temp.textContent = oneDecimal(temp);
  dom.humidity.textContent = oneDecimal(humidity);
  dom.dsTemp.textContent = oneDecimal(dsTemp);
  dom.rpmValue.textContent = whole(rpm);
  dom.speedValue.textContent = whole(speed);
  dom.profileTag.textContent = profile.replace("_", " ");
  dom.cmdSeq.textContent = whole(toNum(data.cmd_seq));
  dom.cmdName.textContent = String(data.last_cmd || "--");
  dom.cmdAge.textContent = commandAgeMs === null ? "--" : (Math.round(commandAgeMs) + " ms");
  dom.shtStatus.textContent = data.sht_ok ? "I2C sensor status: connected" : "I2C sensor status: not detected";
  dom.comfortLine.textContent = comfort === null ? "Comfort score: --" : ("Comfort score: " + comfort + "/100");

  if (!sliderPointerActive) {
    dom.speedSlider.value = speed === null ? 0 : Math.round(speed);
  }
  updateFanOrb(speed === null ? 0 : speed);

  if (dom.modeSelect.value !== mode) dom.modeSelect.value = mode;
  if (dom.profileSelect.value !== profile) dom.profileSelect.value = profile;
  dom.speedSlider.disabled = mode !== "manual";
  setActiveQuickMode(mode);

  lastFetchTs = Date.now();
  setConnectionState(true);
  dom.statusNote.textContent = mode === "manual"
    ? "Manual speed path is active."
    : "Auto curve is active in firmware.";
}

async function requestJSON(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error("HTTP " + response.status);
  }
  return response.json();
}

async function refreshData() {
  if (requestRunning) return;
  requestRunning = true;
  try {
    const data = await requestJSON("/data");
    renderState(data);
  } catch (_error) {
    setConnectionState(false);
    dom.statusNote.textContent = "Connection lost. Retrying...";
  } finally {
    requestRunning = false;
  }
}

function refreshAgeTicker() {
  if (!lastFetchTs) {
    dom.refreshAge.textContent = "--";
    return;
  }
  const seconds = Math.max(0, Math.round((Date.now() - lastFetchTs) / 1000));
  dom.refreshAge.textContent = seconds + "s ago";
}

async function setMode(value) {
  try {
    const state = await requestJSON("/mode?value=" + encodeURIComponent(value));
    renderState(state);
  } catch (_error) {
    setConnectionState(false);
  }
}

async function setProfile(value) {
  try {
    const state = await requestJSON("/profile?value=" + encodeURIComponent(value));
    renderState(state);
  } catch (_error) {
    setConnectionState(false);
  }
}

async function toggleMode() {
  try {
    const state = await requestJSON("/toggle");
    renderState(state);
  } catch (_error) {
    setConnectionState(false);
  }
}

async function sendSpeed(speed) {
  if (latestMode !== "manual") return;
  try {
    const state = await requestJSON("/set?speed=" + encodeURIComponent(speed));
    renderState(state);
  } catch (_error) {
    setConnectionState(false);
  }
}

dom.modeSelect.addEventListener("change", function (event) {
  setMode(event.target.value);
});

dom.profileSelect.addEventListener("change", function (event) {
  setProfile(event.target.value);
});

dom.manualBtn.addEventListener("click", function () {
  setMode("manual");
});

dom.classicBtn.addEventListener("click", function () {
  setMode("classic_auto");
});

dom.aiBtn.addEventListener("click", function () {
  setMode("ai_assist");
});

dom.toggleBtn.addEventListener("click", function () {
  toggleMode();
});

dom.speedSlider.addEventListener("pointerdown", function () {
  sliderPointerActive = true;
});

window.addEventListener("pointerup", function () {
  sliderPointerActive = false;
});

dom.speedSlider.addEventListener("input", function (event) {
  const speed = Math.max(0, Math.min(100, Number(event.target.value) || 0));
  dom.speedValue.textContent = whole(speed);
  updateFanOrb(speed);

  if (latestMode !== "manual") return;
  clearTimeout(sliderDebounce);
  sliderDebounce = setTimeout(function () {
    sendSpeed(speed);
  }, 120);
});

dom.speedSlider.addEventListener("change", function (event) {
  if (latestMode !== "manual") return;
  const speed = Math.max(0, Math.min(100, Number(event.target.value) || 0));
  sendSpeed(speed);
});

setInterval(refreshData, 1200);
setInterval(refreshAgeTicker, 1000);
refreshData();
refreshAgeTicker();
</script>
</body>
</html>
)rawliteral";

// ===== ROUTES =====
void handleRoot() {
    server.send(200, "text/html", webpage);
}

void handleData() {
    server.send(200, "application/json", getJSON());
}

void handleSet() {
    if (!modeIsAutomatic(controlMode) && server.hasArg("speed")) {
        int speed = server.arg("speed").toInt();
        setFanSpeed(0, speed);
        autoAppliedSpeed = fanSpeed[0];
        lastCommandMs = millis();
        commandSeq++;
        lastCommand = "set";
    }
    server.send(200, "application/json", getJSON());
}

void handleToggle() {
    if (modeIsAutomatic(controlMode)) {
        controlMode = CONTROL_MODE_MANUAL;
    } else {
        controlMode = CONTROL_MODE_CLASSIC_AUTO;
        syncAutoAppliedSpeedToCurrent();
    }
    lastCommandMs = millis();
    commandSeq++;
    lastCommand = "toggle";
    server.send(200, "application/json", getJSON());
}

void handleMode() {
    if (server.hasArg("value")) {
        controlMode = parseControlMode(server.arg("value"));
    } else if (server.hasArg("mode")) {
        controlMode = parseControlMode(server.arg("mode"));
    }

    if (modeIsAutomatic(controlMode)) {
        syncAutoAppliedSpeedToCurrent();
    }

    lastCommandMs = millis();
    commandSeq++;
    lastCommand = "mode";
    server.send(200, "application/json", getJSON());
}

void handleProfile() {
    if (server.hasArg("value")) {
        controlProfileIndex = parseControlProfileIndex(server.arg("value"));
    } else if (server.hasArg("profile")) {
        controlProfileIndex = parseControlProfileIndex(server.arg("profile"));
    }

    syncAutoAppliedSpeedToCurrent();
    lastCommandMs = millis();
    commandSeq++;
    lastCommand = "profile";
    server.send(200, "application/json", getJSON());
}

void handleState() {
    server.send(200, "application/json", getJSON());
}

void setupOTA() {
    ArduinoOTA.setHostname(otaHostname);
    ArduinoOTA.setPassword(otaPassword);

    ArduinoOTA.onStart([]() {
        Serial.println("OTA update started");
    });

    ArduinoOTA.onEnd([]() {
        Serial.println("\nOTA update finished");
    });

    ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
        Serial.printf("OTA progress: %u%%\r", (progress * 100) / total);
    });

    ArduinoOTA.onError([](ota_error_t error) {
        Serial.printf("OTA error [%u]\n", error);
    });

    ArduinoOTA.begin();
    Serial.printf("OTA ready at %s.local\n", otaHostname);
}

// ===== SETUP =====
void setup() {
    Serial.begin(115200);

    sensors.begin();
    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
    shtOnline = sht30.begin(0x44);
    if (!shtOnline) shtOnline = sht30.begin(0x45);
    Serial.println(shtOnline ? "SHT30 ready on I2C" : "SHT30 not found on I2C");

    for (int i=0;i<FAN_COUNT;i++) {
        ledcSetup(i, PWM_FREQ, PWM_RES);
        ledcAttachPin(pwmPins[i], i);
        setFanSpeed(i, activeProfile().minSpeed);
    }
    syncAutoAppliedSpeedToCurrent();

    pinMode(tachPins[0], INPUT_PULLUP);
    attachInterrupt(digitalPinToInterrupt(tachPins[0]), tach0, FALLING);

    WiFi.begin(ssid, password);
    while (WiFi.status() != WL_CONNECTED) delay(500);

    Serial.println(WiFi.localIP());
    setupOTA();

    server.on("/", handleRoot);
    server.on("/data", handleData);
    server.on("/state", handleState);
    server.on("/set", handleSet);
    server.on("/toggle", handleToggle);
    server.on("/mode", handleMode);
    server.on("/profile", handleProfile);
    server.begin();
}

// ===== LOOP =====
void loop() {
    ArduinoOTA.handle();
    server.handleClient();

    static uint32_t last = 0;
    uint32_t nowMs = millis();
    if (nowMs - last >= 1000) {
        uint32_t elapsedMs = nowMs - last;
        last = nowMs;

        sensors.requestTemperatures();
        dsTemperatureC = sensors.getTempCByIndex(0);

        if (shtOnline) {
            float t = sht30.readTemperature();
            float h = sht30.readHumidity();
            if (!isnan(t)) shtTemperatureC = t;
            if (!isnan(h)) humidityRH = h;
        }

        for (int i=0;i<FAN_COUNT;i++) {
            noInterrupts();
            uint32_t count = tachCount[i];
            tachCount[i] = 0;
            interrupts();

            float pulsesPerSecond = (count * 1000.0f) / (float)elapsedMs;
            float rawRpm = (pulsesPerSecond * 60.0f) / FAN_TACH_PULSES_PER_REV;
            rawRpm = constrain(rawRpm, 0.0f, (float)FAN_MAX_VALID_RPM);

            // Smooth tach feedback so UI does not jump on occasional pulse jitter.
            const float alpha = 0.35f;
            rpmFiltered[i] = (alpha * rawRpm) + ((1.0f - alpha) * rpmFiltered[i]);
            rpm[i] = (uint32_t)(rpmFiltered[i] + 0.5f);

            if (modeIsAutomatic(controlMode)) {
                const FanProfile& profile = activeProfile();
                float controlTemp = !isnan(shtTemperatureC) ? shtTemperatureC : dsTemperatureC;
                uint8_t target = calculateAutoTargetSpeed(controlTemp, humidityRH, controlMode, profile);
                uint8_t applied = applyAutoSlew(target, profile);
                setFanSpeed(i, applied);
            }
        }
    }
}
