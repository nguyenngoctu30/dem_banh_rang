#include <WiFi.h>
#include <PubSubClient.h>
#include <ctype.h>
#include <string.h>

const char* WIFI_SSID = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

const char* MQTT_HOST = "broker.hivemq.com";
const uint16_t MQTT_PORT = 1883;
const char* MQTT_TOPIC = "banhrang/conveyor/control";

const uint8_t CONVEYOR_PIN = 4;
const unsigned long RECONNECT_INTERVAL_MS = 5000;

WiFiClient networkClient;
PubSubClient mqttClient(networkClient);
String mqttClientId;
unsigned long lastWifiAttempt = 0;
unsigned long lastMqttAttempt = 0;
bool conveyorIsOn = false;

void setConveyor(bool isOn) {
  if (conveyorIsOn == isOn) {
    return;
  }

  conveyorIsOn = isOn;
  digitalWrite(CONVEYOR_PIN, isOn ? HIGH : LOW);
  Serial.printf("Conveyor output: %s (GPIO%d=%s)\n",
                isOn ? "ON" : "OFF",
                CONVEYOR_PIN,
                isOn ? "HIGH" : "LOW");
}

void onMqttMessage(char* topic, byte* payload, unsigned int length) {
  if (strcmp(topic, MQTT_TOPIC) != 0 || length == 0 || length >= 8) {
    return;
  }

  char command[8];
  for (unsigned int i = 0; i < length; i++) {
    command[i] = static_cast<char>(tolower(payload[i]));
  }
  command[length] = '\0';

  if (strcmp(command, "on") == 0) {
    setConveyor(true);
  } else if (strcmp(command, "off") == 0) {
    setConveyor(false);
  } else {
    Serial.printf("Ignored MQTT command: %s\n", command);
  }
}

void connectToMqtt() {
  if (WiFi.status() != WL_CONNECTED || mqttClient.connected()) {
    return;
  }

  if (millis() - lastMqttAttempt < RECONNECT_INTERVAL_MS) {
    return;
  }
  lastMqttAttempt = millis();

  Serial.printf("Connecting to MQTT %s:%u...\n", MQTT_HOST, MQTT_PORT);
  if (mqttClient.connect(mqttClientId.c_str())) {
    Serial.println("MQTT connected");
    if (mqttClient.subscribe(MQTT_TOPIC, 0)) {
      Serial.printf("Subscribed to %s\n", MQTT_TOPIC);
    } else {
      Serial.println("MQTT subscribe failed");
      mqttClient.disconnect();
    }
  } else {
    Serial.printf("MQTT connection failed, state=%d\n", mqttClient.state());
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(CONVEYOR_PIN, OUTPUT);
  digitalWrite(CONVEYOR_PIN, LOW);
  conveyorIsOn = false;

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  String macAddress = WiFi.macAddress();
  macAddress.replace(":", "");
  mqttClientId = "gear-esp32-" + macAddress;

  mqttClient.setServer(MQTT_HOST, MQTT_PORT);
  mqttClient.setCallback(onMqttMessage);
  mqttClient.setBufferSize(256);
  mqttClient.setKeepAlive(30);

  Serial.println("ESP32 conveyor MQTT controller started; GPIO4 is LOW");
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    setConveyor(false);
    if (mqttClient.connected()) {
      mqttClient.disconnect();
    }

    if (millis() - lastWifiAttempt >= RECONNECT_INTERVAL_MS) {
      lastWifiAttempt = millis();
      Serial.println("Wi-Fi disconnected; reconnecting...");
      WiFi.reconnect();
    }
    delay(10);
    return;
  }

  if (!mqttClient.connected()) {
    setConveyor(false);
    connectToMqtt();
    delay(10);
    return;
  }

  mqttClient.loop();
}
