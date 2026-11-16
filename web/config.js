window.APP_CONFIG = {
  api: {
    // Domain API (ngrok tunnel tro toi http://localhost:8000), KHONG co dau "/" o cuoi
    baseUrl: "https://germicide-stout-icy.ngrok-free.dev",
    paths: {
      status: "/api/status",
      camera: "/api/camera/stream",
      yolo: "/api/camera/yolo"
    },
    pollIntervalMs: 1200,
    // Header de bo qua trang canh bao cua ngrok ban mien phi
    headers: {
      "ngrok-skip-browser-warning": "true"
    }
  },
  mqtt: {
    brokerUrl: "wss://broker.hivemq.com:8884/mqtt",
    username: "",
    password: "",
    clientIdPrefix: "gear-web",
    controlTopic: "banhrang/conveyor/control",
    speedTopic: "banhrang/conveyor/speed",
    stateTopic: "banhrang/conveyor/state",
    payloadOn: "on",
    payloadOff: "off"
  }
};

// Ghep baseUrl + path thanh URL day du
window.apiUrl = function (key) {
  const { baseUrl, paths } = window.APP_CONFIG.api;
  return baseUrl.replace(/\/+$/, "") + paths[key];
};

// fetch co san header ngrok, tra ve JSON
window.apiFetch = async function (key, options = {}) {
  const { headers } = window.APP_CONFIG.api;
  const res = await fetch(window.apiUrl(key), {
    ...options,
    headers: { ...headers, ...(options.headers || {}) }
  });
  if (!res.ok) throw new Error("HTTP " + res.status + " khi goi " + key);
  return res.json();
};

// Tao clientId MQTT ngau nhien de nhieu tab khong dam nhau
window.mqttClientId = function () {
  return window.APP_CONFIG.mqtt.clientIdPrefix + "-" + Math.random().toString(16).slice(2, 10);
};
