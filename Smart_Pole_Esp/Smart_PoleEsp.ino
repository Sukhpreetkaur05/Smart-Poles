#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

// ===== WIFI =====
const char* WIFI_SSID = "Wifiweb";
const char* WIFI_PASS = "12345678";

// Laptop IPv4 (run `ipconfig`). Port 5002 = ESP_PORT in app.py
const char* PYTHON_URL = "http://10.202.119.229:5002/esp";

#define LCD_ADDR 0x27

// ===== LDR CALIBRATION =====
const int ADC_DARK = 500;
const int ADC_BRIGHT = 3000;
const int NIGHT_PCT = 50;
const int DAY_PCT = 5;
const int NIGHT_ADC_BELOW = 1200;   // raw ADC below this = night (tune)

// ===== PINS =====
const int BLUE_PINS[3] = {25, 33, 14};
#define PIN_RED    32
#define PIN_LDR    34
#define PIN_PIR    27
#define PIN_SOS     4
#define PIN_BUZZER 26

LiquidCrystal_I2C lcd(LCD_ADDR, 16, 2);
WebServer server(80);

// ===== STATE (shared between loop and network task -> volatile) =====
volatile bool sosActive = false;
volatile bool silentMode = false;
volatile bool motionNow = false;
volatile bool nightNow = false;
bool armed = true;

volatile unsigned long sosSeq = 0;   // +1 per SOS press -> Python starts recording
volatile bool pollNow = false;       // send to Python immediately
unsigned long motionUntil = 0;
unsigned long redBlinkUntil = 0;

int ldrRaw = 0;
float ldrSmooth = 0;
int ambientPct = 0;
int bluePct = 0;

// ===== PYTHON ALERT STATE =====
char remoteAlertBuf[24] = "";
volatile unsigned long remoteAlertUntil = 0;
portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
unsigned long lastPoll = 0;
const unsigned long POLL_INTERVAL = 1000;

void setRemoteAlert(const char* s) {
  portENTER_CRITICAL(&mux);
  strncpy(remoteAlertBuf, s, sizeof(remoteAlertBuf) - 1);
  remoteAlertBuf[sizeof(remoteAlertBuf) - 1] = 0;
  portEXIT_CRITICAL(&mux);
}

void getRemoteAlert(char* out, size_t n) {
  portENTER_CRITICAL(&mux);
  strncpy(out, remoteAlertBuf, n - 1);
  out[n - 1] = 0;
  portEXIT_CRITICAL(&mux);
}

// ===== BUTTON STATE =====
// Single click = loud SOS, Double click = silent SOS, Hold 3 s = cancel
const unsigned long DOUBLE_MS = 400;
const unsigned long HOLD_CANCEL_MS = 3000;
bool lastRaw = HIGH;
bool stableBtn = HIGH;
bool holdHandled = false;
unsigned long lastChange = 0;
unsigned long pressStart = 0;
unsigned long lastRelease = 0;
int clicks = 0;

// ===== OUTPUT STATE =====
unsigned long tFlick = 0;
unsigned long tBeep = 0;
bool redState = false;
bool buzState = false;

// ===== BLUE LED PWM =====
void pwmInit() {
  for (int i = 0; i < 3; i++) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcAttach(BLUE_PINS[i], 5000, 8);
#else
    ledcSetup(i, 5000, 8);
    ledcAttachPin(BLUE_PINS[i], i);
#endif
  }
}

void setBlue(int pct) {
  pct = constrain(pct, 0, 100);
  bluePct = pct;
  int duty = 255 - (pct * 255 / 100);   // active-low
  for (int i = 0; i < 3; i++) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcWrite(BLUE_PINS[i], duty);
#else
    ledcWrite(i, duty);
#endif
  }
}

// ===== LCD =====
void lcdPrint(const char* l1, const char* l2) {
  char a[17], b[17];
  snprintf(a, sizeof(a), "%-16.16s", l1);
  snprintf(b, sizeof(b), "%-16.16s", l2);
  lcd.setCursor(0, 0);
  lcd.print(a);
  lcd.setCursor(0, 1);
  lcd.print(b);
}

void updateLCD() {
  if (sosActive && !silentMode) {
    lcdPrint("LOCAL SOS ACTIVE", "Help requested");
  } else if (sosActive && silentMode) {
    lcdPrint("SILENT SOS", "Help requested");
  } else if (millis() < remoteAlertUntil) {
    char r[24];
    getRemoteAlert(r, sizeof(r));
    lcdPrint("REMOTE SOS ALERT", r);
  } else if (WiFi.status() != WL_CONNECTED) {
    lcdPrint("WiFi disconnected", "Check connection");
  } else {
    char line[17];
    snprintf(line, sizeof(line), "Light:%d%% ONLINE", bluePct);
    lcdPrint("Smart Pole 1", line);
  }
}

// ===== LOCAL SOS =====
void triggerSOS(bool silent) {
  sosActive = true;
  silentMode = silent;
  sosSeq++;
  pollNow = true;                    // network task sends it to Python
  if (silent) redBlinkUntil = millis() + 300;

  Serial.println(silent ? "[SOS] SILENT SOS STARTED" : "[SOS] LOUD SOS STARTED");
  updateLCD();                       // show on LCD immediately
}

void cancelSOS() {
  if (!sosActive) return;
  sosActive = false;
  silentMode = false;
  motionUntil = 0;
  redBlinkUntil = 0;
  digitalWrite(PIN_BUZZER, LOW);
  digitalWrite(PIN_RED, LOW);
  Serial.println("[SOS] LOCAL SOS CANCELLED");
}

// ===== SOS BUTTON =====
void handleButton() {
  bool raw = digitalRead(PIN_SOS);
  unsigned long now = millis();

  if (raw != lastRaw) {
    lastChange = now;
    lastRaw = raw;
  }

  // Debounce
  if (now - lastChange > 30 && raw != stableBtn) {
    stableBtn = raw;

    if (stableBtn == LOW) {
      pressStart = now;
      holdHandled = false;
      Serial.println("[BTN] pressed");
    } else {
      Serial.println("[BTN] released");
      if (!holdHandled && (now - pressStart) < HOLD_CANCEL_MS) {
        clicks++;
        lastRelease = now;
      }
    }
  }

  // 3 second hold = cancel
  if (stableBtn == LOW && !holdHandled && now - pressStart >= HOLD_CANCEL_MS) {
    holdHandled = true;
    clicks = 0;
    cancelSOS();
  }

  // single / double click decision
  if (clicks > 0 && stableBtn == HIGH && now - lastRelease > DOUBLE_MS) {
    if (!sosActive) triggerSOS(clicks >= 2);
    clicks = 0;
  }
}

// ===== LDR =====
void readLDR() {
  ldrRaw = analogRead(PIN_LDR);
  ldrSmooth = ldrSmooth * 0.9f + ldrRaw * 0.1f;

  float x = constrain(ldrSmooth, ADC_DARK, ADC_BRIGHT);
  ambientPct = NIGHT_PCT +
      (int)((x - ADC_DARK) * (DAY_PCT - NIGHT_PCT) / (ADC_BRIGHT - ADC_DARK));
  ambientPct = constrain(ambientPct, 0, 100);

  if (ldrSmooth < NIGHT_ADC_BELOW - 100) nightNow = true;
  else if (ldrSmooth > NIGHT_ADC_BELOW + 100) nightNow = false;
}

// ===== PYTHON SERVER POLLING (runs in its own task) =====
void pollPythonServer() {
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  http.setConnectTimeout(1000);
  http.setTimeout(1500);

  if (!http.begin(PYTHON_URL)) {
    Serial.println("[PYTHON] HTTP begin failed");
    return;
  }
  http.addHeader("Content-Type", "application/json");

  String payload =
      "{\"armed\":" + String(armed ? 1 : 0) +
      ",\"pir\":" + String(motionNow ? 1 : 0) +
      ",\"ldr\":" + String(nightNow ? 1 : 0) +
      ",\"sos_seq\":" + String(sosSeq) + "}";

  int code = http.POST(payload);

  if (code > 0) {
    String response = http.getString();
    response.trim();
    Serial.printf("[PYTHON] HTTP %d: %s\n", code, response.c_str());

    if (code == 200 && response.startsWith("ALERT|")) {
      String a = response.substring(6);
      a.trim();
      a.replace("|", " ");
      setRemoteAlert(a.c_str());
      remoteAlertUntil = millis() + 10000;
      Serial.println("[ALERT] Python incident received!");
    }
  } else {
    Serial.printf("[PYTHON] Request failed: %s\n", http.errorToString(code).c_str());
  }
  http.end();
}

void netTask(void* param) {
  for (;;) {
    unsigned long now = millis();
    if (pollNow || now - lastPoll >= POLL_INTERVAL) {
      lastPoll = now;
      pollNow = false;
      pollPythonServer();           // may block, but only THIS task
    }
    vTaskDelay(pdMS_TO_TICKS(50));
  }
}

// ===== LOCAL WEB SERVER =====
void handleStatus() {
  char r[24];
  getRemoteAlert(r, sizeof(r));
  String j = "{\"pole\":1";
  j += ",\"ldr\":" + String(ldrRaw);
  j += ",\"night\":" + String(nightNow ? 1 : 0);
  j += ",\"ambient\":" + String(ambientPct);
  j += ",\"sos\":" + String(sosActive ? 1 : 0);
  j += ",\"silent\":" + String(silentMode ? 1 : 0);
  j += ",\"seq\":" + String(sosSeq);
  j += ",\"motion\":" + String(motionNow ? 1 : 0);
  j += ",\"armed\":" + String(armed ? 1 : 0);
  j += ",\"remoteAlert\":\"" + String(r) + "\"}";
  server.send(200, "application/json", j);
}

void handleCancel() {
  cancelSOS();
  remoteAlertUntil = 0;
  setRemoteAlert("");
  server.send(200, "text/plain", "ok");
}

void handleTest() {
  bool silent = server.arg("silent") == "1";
  triggerSOS(silent);
  server.send(200, "text/plain", "ok");
}

// ===== SETUP =====
void setup() {
  Serial.begin(115200);

  pinMode(PIN_RED, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_SOS, INPUT_PULLUP);
  pinMode(PIN_LDR, INPUT);

  digitalWrite(PIN_RED, LOW);
  digitalWrite(PIN_BUZZER, LOW);

  analogReadResolution(12);
  analogSetPinAttenuation(PIN_LDR, ADC_11db);

  pwmInit();
  setBlue(0);

  Wire.begin(21, 22);
  lcd.init();
  lcd.backlight();
  lcdPrint("Smart Pole 1", "WiFi connecting");

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(300);
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("ESP32 IP: ");
    Serial.println(WiFi.localIP());
    String ip = WiFi.localIP().toString();
    lcdPrint("WiFi Connected", ip.c_str());
  } else {
    Serial.println("WiFi connection failed");
    lcdPrint("WiFi FAIL", "Check credentials");
  }
  delay(1500);

  server.on("/status", handleStatus);
  server.on("/cancel", handleCancel);
  server.on("/test", handleTest);
  server.begin();

  ldrSmooth = analogRead(PIN_LDR);
  nightNow = ldrSmooth < NIGHT_ADC_BELOW;

  // Buzzer + red LED startup test (you should HEAR and SEE this)
  digitalWrite(PIN_BUZZER, HIGH);
  digitalWrite(PIN_RED, HIGH);
  delay(200);
  digitalWrite(PIN_BUZZER, LOW);
  digitalWrite(PIN_RED, LOW);

  // Network polling in its own task on core 0 -> never blocks button/LCD/buzzer
  xTaskCreatePinnedToCore(netTask, "net", 8192, NULL, 1, NULL, 0);

  Serial.println("Smart Pole ready");
}

// ===== MAIN LOOP =====
unsigned long tLDR = 0;
unsigned long tLCD = 0;

void loop() {
  unsigned long now = millis();

  server.handleClient();
  handleButton();

  if (now - tLDR >= 50) {
    tLDR = now;
    readLDR();
  }

  // PIR is monitored only after local SOS
  motionNow = false;
  if (sosActive && digitalRead(PIN_PIR) == HIGH) {
    motionNow = true;
    motionUntil = now + 5000;
  }

  // Blue LEDs
  if (sosActive && !silentMode) setBlue(100);
  else if (sosActive && now < motionUntil) setBlue(100);
  else setBlue(ambientPct);

  // Red LED
  if (sosActive && !silentMode) {
    if (now - tFlick >= 100) {
      tFlick = now;
      redState = !redState;
      digitalWrite(PIN_RED, redState);
    }
  } else {
    digitalWrite(PIN_RED, now < redBlinkUntil ? HIGH : LOW);
  }

  // Buzzer: loud local SOS or Python incident
  bool remoteActive = now < remoteAlertUntil;
  bool alarm = (sosActive && !silentMode) || remoteActive;

  if (alarm) {
    if (now - tBeep >= 250) {
      tBeep = now;
      buzState = !buzState;
      digitalWrite(PIN_BUZZER, buzState);
    }
  } else {
    buzState = false;
    digitalWrite(PIN_BUZZER, LOW);
  }

  if (now - tLCD >= 300) {
    tLCD = now;
    updateLCD();
  }
}