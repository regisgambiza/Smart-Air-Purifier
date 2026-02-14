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
uint32_t rpm[FAN_COUNT] = {0};
uint8_t fanSpeed[FAN_COUNT] = {40};

bool autoMode = true;
float dsTemperatureC = 0;
float shtTemperatureC = NAN;
float humidityRH = NAN;
bool shtOnline = false;

// ===== INTERRUPTS =====
void IRAM_ATTR tach0() { tachCount[0]++; }

// ===== SET FAN SPEED =====
void setFanSpeed(int fan, uint8_t percent) {
    percent = constrain(percent, 0, 100);
    fanSpeed[fan] = percent;
    uint32_t duty = map(percent, 0, 100, 0, 255);
    ledcWrite(fan, duty);
}

// ===== SIMPLE FAN CURVE =====
uint8_t calculateFanFromTemp(float temp) {
    if (temp < 30) return 30;
    if (temp < 40) return 50;
    if (temp < 50) return 70;
    return 100;
}

// ===== JSON STATUS =====
String getJSON() {
    String json = "{";
    json += "\"temp\":" + String(shtTemperatureC, 1) + ",";
    json += "\"humidity\":" + String(humidityRH, 1) + ",";
    json += "\"ds_temp\":" + String(dsTemperatureC, 1) + ",";
    json += "\"sht_ok\":" + String(shtOnline ? "true" : "false") + ",";
    json += "\"auto\":" + String(autoMode ? "true" : "false") + ",";
    json += "\"rpm\":" + String(rpm[0]) + ",";
    json += "\"speed\":" + String(fanSpeed[0]);
    json += "}";
    return json;
}

// ===== WEB PAGE =====
const char webpage[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Smart Air Purifier</title>
<style>
:root {
  --bg-1: #0b132b;
  --bg-2: #1c2541;
  --card: rgba(255, 255, 255, 0.1);
  --line: rgba(255, 255, 255, 0.18);
  --text: #f7f9fc;
  --muted: #b6c2d9;
  --accent: #38bdf8;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
  color: var(--text);
  background:
    radial-gradient(circle at 10% 20%, #2c4a86 0%, transparent 45%),
    radial-gradient(circle at 90% 10%, #1f8a70 0%, transparent 38%),
    linear-gradient(145deg, var(--bg-1), var(--bg-2));
  min-height: 100vh;
}
.wrap {
  max-width: 900px;
  margin: 0 auto;
  padding: 18px;
}
.title {
  font-size: 1.6rem;
  font-weight: 700;
  letter-spacing: 0.4px;
  margin: 6px 0 16px;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px;
}
.card {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 16px;
  backdrop-filter: blur(7px);
}
.label {
  color: var(--muted);
  font-size: 0.84rem;
  letter-spacing: 0.4px;
  text-transform: uppercase;
}
.value {
  font-size: 1.65rem;
  font-weight: 700;
  margin-top: 8px;
}
.status {
  display: inline-block;
  margin-top: 8px;
  padding: 5px 10px;
  border-radius: 999px;
  font-size: 0.78rem;
  font-weight: 700;
  border: 1px solid transparent;
}
.status.auto {
  color: #dcfce7;
  background: rgba(34, 197, 94, 0.2);
  border-color: rgba(34, 197, 94, 0.55);
}
.status.manual {
  color: #dbeafe;
  background: rgba(56, 189, 248, 0.2);
  border-color: rgba(56, 189, 248, 0.55);
}
.control {
  margin-top: 12px;
}
.slider {
  width: 100%;
  accent-color: var(--accent);
}
.small {
  color: var(--muted);
  font-size: 0.9rem;
}
button {
  margin-top: 12px;
  width: 100%;
  border: 1px solid rgba(255, 255, 255, 0.22);
  color: var(--text);
  background: rgba(255, 255, 255, 0.12);
  border-radius: 10px;
  padding: 10px 12px;
  font-weight: 600;
  cursor: pointer;
}
button:hover { background: rgba(255, 255, 255, 0.2); }
</style>
</head>
<body>
<div class="wrap">
<div class="title">Smart Air Purifier</div>
<div class="grid">
  <div class="card">
    <div class="label">Air Purifier</div>
    <div id="modeText" class="value">--</div>
    <span id="modeBadge" class="status">--</span>
    <div class="label" style="margin-top:14px;">Fan Speed</div>
    <div class="value"><span id="speedLabel">--</span>%</div>
    <div class="control">
      <input id="speedSlider" type="range" min="0" max="100" value="40" class="slider" oninput="setFan(this.value)">
    </div>
    <div id="controlHint" class="small">Manual control enabled</div>
    <div class="small" style="margin-top:10px;">Fan RPM: <span id="rpm">--</span></div>
    <button onclick="toggleMode()">Toggle Auto/Manual</button>
  </div>

  <div class="card">
    <div class="label">Room Temperature</div>
    <div class="value"><span id="temp">--</span> &deg;C</div>
    <div class="label" style="margin-top:14px;">Room Humidity</div>
    <div class="value"><span id="humidity">--</span> %</div>
    <div class="small">DS18B20: <span id="dsTemp">--</span> &deg;C</div>
    <div id="shtStatus" class="small">I2C sensor status: --</div>
  </div>
</div>
</div>

<script>
function fetchData(){
  fetch("/data").then(r => r.json()).then(data => {
    const tempText = Number.isFinite(data.temp) ? data.temp.toFixed(1) : "--";
    const humidityText = Number.isFinite(data.humidity) ? data.humidity.toFixed(1) : "--";
    const dsTempText = Number.isFinite(data.ds_temp) ? data.ds_temp.toFixed(1) : "--";
    document.getElementById("temp").innerText = tempText;
    document.getElementById("humidity").innerText = humidityText;
    document.getElementById("dsTemp").innerText = dsTempText;
    document.getElementById("rpm").innerText = data.rpm;
    document.getElementById("speedLabel").innerText = data.speed;
    document.getElementById("shtStatus").innerText = data.sht_ok
      ? "I2C sensor status: connected"
      : "I2C sensor status: not detected";

    const slider = document.getElementById("speedSlider");
    slider.value = data.speed;

    const auto = data.auto;
    document.getElementById("modeText").innerText = auto ? "AUTO" : "MANUAL";

    const badge = document.getElementById("modeBadge");
    badge.className = auto ? "status auto" : "status manual";
    badge.innerText = auto ? "Auto curve active" : "Manual fan control";

    document.getElementById("controlHint").innerText = auto
      ? "Disable auto mode to move slider"
      : "Manual control enabled";
    slider.disabled = auto;
  });
}

function setFan(val){
  fetch(`/set?speed=${val}`);
}

function toggleMode(){
  fetch("/toggle");
}

setInterval(fetchData, 1000);
fetchData();
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
    if (!autoMode && server.hasArg("speed")) {
        int speed = server.arg("speed").toInt();
        setFanSpeed(0, speed);
    }
    server.send(200, "text/plain", "OK");
}

void handleToggle() {
    autoMode = !autoMode;
    server.send(200, "text/plain", "OK");
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
        setFanSpeed(i, 40);
    }

    pinMode(tachPins[0], INPUT_PULLUP);
    attachInterrupt(digitalPinToInterrupt(tachPins[0]), tach0, FALLING);

    WiFi.begin(ssid, password);
    while (WiFi.status() != WL_CONNECTED) delay(500);

    Serial.println(WiFi.localIP());
    setupOTA();

    server.on("/", handleRoot);
    server.on("/data", handleData);
    server.on("/set", handleSet);
    server.on("/toggle", handleToggle);
    server.begin();
}

// ===== LOOP =====
void loop() {
    ArduinoOTA.handle();
    server.handleClient();

    static uint32_t last = 0;
    if (millis() - last >= 1000) {

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
            rpm[i] = (count / 2) * 60;

            if (autoMode) {
                float controlTemp = !isnan(shtTemperatureC) ? shtTemperatureC : dsTemperatureC;
                uint8_t autoSpeed = calculateFanFromTemp(controlTemp);
                setFanSpeed(i, autoSpeed);
            }
        }

        last = millis();
    }
}
