#include <WiFi.h>
#include <PubSubClient.h>
#include <ctype.h>
#include <stdlib.h>
#include <string.h>

const char* WIFI_SSID = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

const char* MQTT_HOST = "broker.hivemq.com";
const uint16_t MQTT_PORT = 1883;
const char* CONTROL_TOPIC = "banhrang/conveyor/control";
const char* SPEED_TOPIC = "banhrang/conveyor/speed";
const char* STATE_TOPIC = "banhrang/conveyor/state";

const uint8_t CONVEYOR_PIN = 4;
const uint8_t PWM_PIN = 5;
const uint8_t PWM_MAX = 255;
const unsigned long RECONNECT_INTERVAL_MS = 5000;

WiFiClient networkClient;
PubSubClient mqttClient(networkClient);
String mqttClientId;
unsigned long lastWifiAttempt = 0;
unsigned long lastMqttAttempt = 0;
bool conveyorIsOn = false;
uint8_t conveyorSpeed = 0;

void publishState() {
  char payload[80];
  snprintf(payload, sizeof(payload),
           "{\"conveyor_on\":%s,\"speed\":%u}",
           conveyorIsOn ? "true" : "false",
           conveyorSpeed);
  mqttClient.publish(STATE_TOPIC, payload, true);
}

void setConveyor(bool isOn) {
  if (conveyorIsOn == isOn && digitalRead(CONVEYOR_PIN) == (isOn ? HIGH : LOW)) {
    return;
  }

  conveyorIsOn = isOn;
  digitalWrite(CONVEYOR_PIN, isOn ? HIGH : LOW);
  Serial.printf("Conveyor output: %s (GPIO%d=%s)\n",
                isOn ? "ON" : "OFF",
                CONVEYOR_PIN,
                isOn ? "HIGH" : "LOW");
  publishState();
}

void setConveyorSpeed(uint8_t speed) {
  conveyorSpeed = speed;
  analogWrite(PWM_PIN, conveyorSpeed);
  Serial.printf("Conveyor PWM: %u/255 on GPIO%d\n", conveyorSpeed, PWM_PIN);
  publishState();
}

void onMqttMessage(char* topic, byte* payload, unsigned int length) {
  if (length == 0 || length >= 8) {
    return;
  }

  char command[8];
  for (unsigned int i = 0; i < length; i++) {
    command[i] = static_cast<char>(tolower(payload[i]));
  }
  command[length] = '\0';

  if (strcmp(topic, CONTROL_TOPIC) == 0) {
    if (strcmp(command, "on") == 0) {
      setConveyor(true);
    } else if (strcmp(command, "off") == 0) {
      setConveyor(false);
    } else {
      Serial.printf("Ignored conveyor command: %s\n", command);
    }
    return;
  }

  if (strcmp(topic, SPEED_TOPIC) == 0) {
    char* end = nullptr;
    const long parsedSpeed = strtol(command, &end, 10);
    if (*end != '\0' || parsedSpeed < 0 || parsedSpeed > PWM_MAX) {
      Serial.printf("Ignored invalid PWM command: %s\n", command);
      return;
    }
    if (parsedSpeed != 0 && parsedSpeed != 85 && parsedSpeed != 170 && parsedSpeed != 255) {
      Serial.printf("Ignored unsupported speed level: %ld\n", parsedSpeed);
      return;
    }
    setConveyorSpeed(static_cast<uint8_t>(parsedSpeed));
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
    const bool controlSubscribed = mqttClient.subscribe(CONTROL_TOPIC, 0);
    const bool speedSubscribed = mqttClient.subscribe(SPEED_TOPIC, 0);
    if (controlSubscribed && speedSubscribed) {
      Serial.printf("Subscribed to %s and %s\n", CONTROL_TOPIC, SPEED_TOPIC);
      publishState();
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
  pinMode(PWM_PIN, OUTPUT);
  digitalWrite(CONVEYOR_PIN, LOW);
  analogWrite(PWM_PIN, 0);
  conveyorIsOn = false;
  conveyorSpeed = 0;

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

  Serial.println("ESP32 conveyor MQTT controller started; GPIO4=LOW, GPIO5 PWM=0");
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
