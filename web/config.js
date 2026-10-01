window.APP_CONFIG = {
  api: {
    // Dien domain API, vi du: https://api.example.com 
    baseUrl: "",
    paths: {
      status: "/api/status",
      camera: "/api/camera/stream",
      yolo: "/api/camera/yolo"
    },
    pollIntervalMs: 1200
  },
  mqtt: {
    brokerUrl: "wss://broker.hivemq.com:8884/mqtt",
    username: "",
    password: "",
    clientIdPrefix: "gear-web",
    controlTopic: "banhrang/conveyor/control",
    speedTopic: "banhrang/conveyor/speed",
    payloadOn: "on",
    payloadOff: "off",
    stateTopic: "banhrang/conveyor/state"
  }
};
