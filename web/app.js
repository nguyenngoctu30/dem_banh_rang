(() => {
  const config = window.APP_CONFIG || {};
  const apiConfig = config.api || {};
  const mqttConfig = config.mqtt || {};
  const API_STORAGE_KEY = "gear-web-api-base";

  // Chuan hoa link: them https:// neu thieu, bo path va dau "/" cuoi. Tra ve "" neu khong hop le.
  function normalizeBaseUrl(raw) {
    let value = String(raw || "").trim();
    if (!value) return "";
    if (!/^https?:\/\//i.test(value)) value = `https://${value}`;
    try {
      return new URL(value).origin;
    } catch (_) {
      return "";
    }
  }

  function loadSavedApiBase() {
    try {
      return window.localStorage.getItem(API_STORAGE_KEY) || "";
    } catch (_) {
      return "";
    }
  }

  function saveApiBase(value) {
    try {
      if (value) window.localStorage.setItem(API_STORAGE_KEY, value);
      else window.localStorage.removeItem(API_STORAGE_KEY);
    } catch (_) {
    }
  }

  // Uu tien: ?api=... tren URL > link da luu tren trinh duyet > config.js (neu co)
  let apiBase = normalizeBaseUrl(
    new URLSearchParams(window.location.search).get("api") ||
    loadSavedApiBase() ||
    apiConfig.baseUrl
  );
  const paths = apiConfig.paths || {};
  // Header bat buoc de ngrok ban mien phi khong tra ve trang canh bao HTML (trang do khong co header CORS)
  const apiHeaders = { "ngrok-skip-browser-warning": "true", ...(apiConfig.headers || {}) };
  const numberFormat = new Intl.NumberFormat("vi-VN");
  const state = {
    source: "stream",
    cameraHidden: false,
    mqttClient: null,
    reportedConveyor: null,
    lastCommand: null,
    selectedSpeed: 255,
    mqttConnected: false,
    cameraLoaded: false,
    streamUrl: null,
    streamController: null,
    frameObjectUrl: null
  };

  const elements = {
    apiForm: document.querySelector("#api-form"),
    apiInput: document.querySelector("#api-input"),
    apiHint: document.querySelector("#api-hint"),
    clock: document.querySelector("#system-clock"),
    apiIndicator: document.querySelector("#api-indicator"),
    apiLabel: document.querySelector("#api-label"),
    mqttIndicator: document.querySelector("#mqtt-indicator"),
    mqttLabel: document.querySelector("#mqtt-label"),
    mqttState: document.querySelector("#mqtt-state"),
    updatedAt: document.querySelector("#updated-at"),
    cameraFeed: document.querySelector("#camera-feed"),
    cameraPlaceholder: document.querySelector("#camera-placeholder"),
    cameraMessage: document.querySelector("#camera-message"),
    cameraDetail: document.querySelector("#camera-detail"),
    cameraLive: document.querySelector("#camera-live"),
    cameraSourceLabel: document.querySelector("#camera-source-label"),
    cameraResolution: document.querySelector("#camera-resolution"),
    cameraToggle: document.querySelector("#camera-toggle"),
    cameraState: document.querySelector("#camera-state"),
    camFps: document.querySelector("#cam-fps"),
    detectFps: document.querySelector("#detect-fps"),
    conveyorState: document.querySelector("#conveyor-state"),
    speedValue: document.querySelector("#speed-value"),
    selectedSpeed: document.querySelector("#selected-speed"),
    qualityPie: document.querySelector("#quality-pie"),
    pieEmptyLabel: document.querySelector("#pie-empty-label"),
    pieTotal: document.querySelector("#pie-total"),
    controlFeedback: document.querySelector("#control-feedback"),
    stopButton: document.querySelector("#stop-button"),
    runButton: document.querySelector("#run-button")
  };

  function formatCount(value) {
    const parsed = Number(value);
    return numberFormat.format(Number.isFinite(parsed) ? parsed : 0);
  }

  function setApiState(status, label) {
    elements.apiIndicator.dataset.state = status;
    elements.apiLabel.textContent = label;
  }

  function setMqttState(status, label) {
    elements.mqttIndicator.dataset.state = status;
    elements.mqttLabel.textContent = label;
    elements.mqttState.textContent = `MQTT ${label.replace("MQTT ", "")}`;
  }

  function showCameraPlaceholder(message, detail) {
    state.cameraLoaded = false;
    elements.cameraFeed.hidden = true;
    elements.cameraPlaceholder.hidden = false;
    elements.cameraMessage.textContent = message;
    elements.cameraDetail.textContent = detail;
    elements.cameraLive.hidden = true;
    elements.cameraResolution.textContent = "NO SIGNAL";
  }

  function updateCameraSource() {
    const isYolo = state.source === "yolo";
    elements.cameraSourceLabel.textContent = isYolo ? "YOLO INFERENCE" : "CAMERA RAW";
    if (state.cameraHidden) {
      showCameraPlaceholder("HÌNH CAMERA ĐANG ẨN", "Chỉ ẩn trên trang web; camera vẫn hoạt động");
      return;
    }
    if (!apiBase) {
      showCameraPlaceholder("ĐANG CHỜ CẤU HÌNH API", "Nhập link backend ở ô phía trên");
      return;
    }
    const path = isYolo ? paths.yolo : paths.camera;
    if (!path) {
      showCameraPlaceholder("THIẾU ĐƯỜNG DẪN CAMERA", "Kiểm tra api.paths trong web/config.js");
      return;
    }
    const nextUrl = `${apiBase}${path}`;
    if (state.streamUrl === nextUrl && state.streamController) return;
    showCameraPlaceholder("ĐANG KẾT NỐI CAMERA", "Đang mở luồng hình ảnh...");
    startCameraStream(nextUrl);
  }

  function stopCameraStream() {
    if (state.streamController) state.streamController.abort();
    state.streamController = null;
    state.streamUrl = null;
    if (state.frameObjectUrl) {
      URL.revokeObjectURL(state.frameObjectUrl);
      state.frameObjectUrl = null;
    }
    elements.cameraFeed.removeAttribute("src");
  }

  function indexOfBytes(buffer, a, b, from) {
    for (let i = from; i < buffer.length - 1; i += 1) {
      if (buffer[i] === a && buffer[i + 1] === b) return i;
    }
    return -1;
  }

  function showFrame(bytes) {
    const nextObjectUrl = URL.createObjectURL(new Blob([bytes], { type: "image/jpeg" }));
    const previous = state.frameObjectUrl;
    state.frameObjectUrl = nextObjectUrl;
    elements.cameraFeed.src = nextObjectUrl;
    if (previous) URL.revokeObjectURL(previous);
  }

  // Doc luong MJPEG bang fetch (de gui duoc header ngrok), tach tung anh JPEG (FFD8...FFD9)
  async function startCameraStream(url) {
    stopCameraStream();
    const controller = new AbortController();
    state.streamController = controller;
    state.streamUrl = url;
    try {
      const response = await fetch(url, { headers: apiHeaders, cache: "no-store", signal: controller.signal });
      if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
      const reader = response.body.getReader();
      let buffer = new Uint8Array(0);
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const merged = new Uint8Array(buffer.length + value.length);
        merged.set(buffer, 0);
        merged.set(value, buffer.length);
        buffer = merged;
        let start = indexOfBytes(buffer, 0xff, 0xd8, 0);
        while (start !== -1) {
          const end = indexOfBytes(buffer, 0xff, 0xd9, start + 2);
          if (end === -1) break;
          showFrame(buffer.slice(start, end + 2));
          buffer = buffer.slice(end + 2);
          start = indexOfBytes(buffer, 0xff, 0xd8, 0);
        }
        if (start === -1 && buffer.length > 2) buffer = buffer.slice(-1);
        if (buffer.length > 4 * 1024 * 1024) buffer = new Uint8Array(0);
      }
      throw new Error("Stream ended");
    } catch (error) {
      if (controller.signal.aborted) return;
      if (state.streamController === controller) {
        state.streamController = null;
        state.streamUrl = null;
      }
      if (!state.cameraHidden) {
        showCameraPlaceholder("KHÔNG NHẬN ĐƯỢC HÌNH ẢNH", "Kiểm tra trạng thái camera và đường dẫn stream");
        window.setTimeout(() => {
          if (!state.cameraHidden && !state.streamController) updateCameraSource();
        }, 3000);
      }
    }
  }

  function setCameraHidden(hidden) {
    state.cameraHidden = hidden;
    elements.cameraToggle.setAttribute("aria-pressed", String(hidden));
    elements.cameraToggle.title = hidden ? "Hiện hình camera trên trang" : "Ẩn hình camera trên trang";
    elements.cameraToggle.querySelector(".toggle-icon-hide").hidden = hidden;
    elements.cameraToggle.querySelector(".toggle-icon-show").hidden = !hidden;
    elements.cameraToggle.querySelector(".toggle-hide-label").hidden = hidden;
    elements.cameraToggle.querySelector(".toggle-show-label").hidden = !hidden;

    if (hidden) {
      stopCameraStream();
      showCameraPlaceholder("HÌNH CAMERA ĐANG ẨN", "Chỉ ẩn trên trang web; camera vẫn hoạt động");
    } else {
      updateCameraSource();
    }
  }

  function updatePieChart(data) {
    const total = Math.max(0, Number(data.self_total) || 0);
    const counts = [
      Math.max(0, Number(data.self_e0_count) || 0),
      Math.max(0, Number(data.self_e1_count) || 0),
      Math.max(0, Number(data.self_e2_count) || 0),
      Math.max(0, Number(data.self_e3_count) || 0)
    ];
    const colors = ["var(--mint)", "var(--amber)", "var(--orange)", "#9b7280"];
    const ids = ["#pie-e0", "#pie-e1", "#pie-e2", "#pie-e3"];
    elements.pieTotal.textContent = formatCount(total);

    counts.forEach((count, index) => {
      const percentage = total > 0 ? (count / total) * 100 : 0;
      document.querySelector(ids[index]).textContent = `${formatCount(count)} · ${percentage.toFixed(1)}%`;
    });

    if (total === 0) {
      elements.qualityPie.style.background = "var(--line-soft)";
      elements.pieEmptyLabel.hidden = false;
      elements.qualityPie.setAttribute("aria-label", "Chưa có dữ liệu biểu đồ");
      return;
    }

    let cursor = 0;
    const slices = counts.map((count, index) => {
      const start = cursor;
      cursor = Math.min(100, cursor + (count / total) * 100);
      return cursor > start ? `${colors[index]} ${start}% ${cursor}%` : null;
    }).filter(Boolean);
    if (cursor < 100) slices.push(`var(--line-soft) ${cursor}% 100%`);
    elements.qualityPie.style.background = `conic-gradient(${slices.join(", ")})`;
    elements.pieEmptyLabel.hidden = true;
    elements.qualityPie.setAttribute("aria-label", `Cơ cấu ${formatCount(total)} sản phẩm: bình thường ${formatCount(counts[0])}, thiếu vòng bi ${formatCount(counts[1])}, sứt mẻ ${formatCount(counts[2])}, bẩn hoặc ố ${formatCount(counts[3])}`);
  }

  function setSelectedSpeed(value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return;
    state.selectedSpeed = Math.max(0, Math.min(255, Math.round(parsed)));
    elements.selectedSpeed.textContent = `${state.selectedSpeed} / 255`;
    elements.speedValue.textContent = String(state.selectedSpeed);
    document.querySelectorAll(".speed-option").forEach((button) => {
      const selected = Number(button.dataset.speed) === state.selectedSpeed;
      button.classList.toggle("is-selected", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
  }

  async function pollStatus() {
    if (!apiBase || !paths.status) {
      setApiState("unset", "API CHƯA CẤU HÌNH");
      elements.updatedAt.textContent = "CHƯA CÓ DỮ LIỆU";
      return;
    }
    const requestBase = apiBase;
    try {
      const response = await fetch(`${requestBase}${paths.status}`, { cache: "no-store", headers: apiHeaders });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (requestBase !== apiBase) return;
      setApiState("online", "API ONLINE");
      document.querySelector("#count-total").textContent = formatCount(data.self_total);
      document.querySelector("#count-normal").textContent = formatCount(data.self_e0_count);
      document.querySelector("#count-defects").textContent = formatCount(data.self_total_err);
      document.querySelector("#count-e1").textContent = formatCount(data.self_e1_count);
      document.querySelector("#count-e2").textContent = formatCount(data.self_e2_count);
      document.querySelector("#count-e3").textContent = formatCount(data.self_e3_count);
      updatePieChart(data);
      elements.camFps.textContent = formatFps(data.cam_fps);
      elements.detectFps.textContent = formatFps(data.detect_fps);
      if (typeof data.speed === "number") setSelectedSpeed(data.speed);
      if (typeof data.conveyor_on === "boolean" && !mqttConfig.stateTopic) {
        state.reportedConveyor = data.conveyor_on;
      }
      updateConveyorDisplay();
      elements.cameraState.textContent = data.camera_running ? "ONLINE" : "OFFLINE";
      elements.cameraState.dataset.state = data.camera_running ? "online" : "offline";
      elements.updatedAt.textContent = `CẬP NHẬT ${new Date().toLocaleTimeString("vi-VN", { hour12: false })}`;
      if (!state.cameraHidden && !data.camera_running && !state.cameraLoaded) {
        showCameraPlaceholder("CAMERA ĐANG TẮT", "Bật camera trong ứng dụng theo dõi");
      }
    } catch (error) {
      if (requestBase !== apiBase) return;
      setApiState("offline", "API OFFLINE");
      elements.updatedAt.textContent = "KHÔNG NHẬN ĐƯỢC DỮ LIỆU";
      elements.cameraState.textContent = "OFFLINE";
      elements.cameraState.dataset.state = "offline";
      if (!state.cameraHidden && !state.cameraLoaded) {
        showCameraPlaceholder("KHÔNG KẾT NỐI ĐƯỢC API", "Kiểm tra địa chỉ API và kết nối mạng");
      }
    }
  }

  function formatFps(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed.toFixed(1) : "--";
  }

  function updateConveyorDisplay() {
    const value = state.reportedConveyor;
    if (typeof value === "boolean") {
      elements.conveyorState.textContent = value ? "RUNNING" : "STOPPED";
      elements.conveyorState.dataset.state = value ? "on" : "off";
      return;
    }
    if (state.lastCommand) {
      elements.conveyorState.textContent = state.lastCommand === "on" ? "LỆNH CHẠY" : "LỆNH DỪNG";
      elements.conveyorState.dataset.state = state.lastCommand;
      return;
    }
    elements.conveyorState.textContent = "CHƯA RÕ";
    delete elements.conveyorState.dataset.state;
  }

  function setMqttButtonsEnabled(enabled) {
    elements.stopButton.disabled = !enabled;
    elements.runButton.disabled = !enabled;
    document.querySelectorAll(".speed-option").forEach((button) => {
      button.disabled = !enabled;
    });
  }

  function isMqttConfigured() {
    return mqttConfig.brokerUrl &&
      !mqttConfig.brokerUrl.includes("YOUR_CLUSTER_ID") &&
      mqttConfig.controlTopic &&
      mqttConfig.speedTopic &&
      mqttConfig.stateTopic;
  }

  function connectMqtt() {
    if (!isMqttConfigured()) {
      setMqttState("unset", "MQTT CHƯA CẤU HÌNH");
      elements.controlFeedback.textContent = "Điền HiveMQ trong web/config.js";
      return;
    }
    if (!window.mqtt || typeof window.mqtt.connect !== "function") {
      setMqttState("offline", "MQTT LIBRARY ERROR");
      elements.controlFeedback.textContent = "Không tải được MQTT.js; cần kết nối Internet";
      return;
    }

    const clientId = `${mqttConfig.clientIdPrefix || "gear-web"}-${Math.random().toString(16).slice(2, 10)}`;
    setMqttState("unset", "MQTT ĐANG KẾT NỐI");
    elements.controlFeedback.textContent = "Đang kết nối HiveMQ...";
    const mqttOptions = {
      clientId,
      clean: true,
      connectTimeout: 8000,
      reconnectPeriod: 3000,
      protocolVersion: 4
    };
    if (mqttConfig.username) mqttOptions.username = mqttConfig.username;
    if (mqttConfig.password) mqttOptions.password = mqttConfig.password;
    state.mqttClient = window.mqtt.connect(mqttConfig.brokerUrl, mqttOptions);

    state.mqttClient.on("connect", () => {
      state.mqttConnected = true;
      setMqttState("online", "MQTT ONLINE");
      setMqttButtonsEnabled(true);
      elements.controlFeedback.textContent = "Sẵn sàng gửi lệnh băng tải";
      if (mqttConfig.stateTopic) {
        state.mqttClient.subscribe(mqttConfig.stateTopic, { qos: 0 }, (error) => {
          if (error) elements.controlFeedback.textContent = `Không đăng ký được state topic: ${error.message}`;
        });
      }
    });
    state.mqttClient.on("reconnect", () => {
      state.mqttConnected = false;
      setMqttState("unset", "MQTT ĐANG KẾT NỐI");
      setMqttButtonsEnabled(false);
    });
    state.mqttClient.on("offline", () => {
      state.mqttConnected = false;
      setMqttState("offline", "MQTT OFFLINE");
      setMqttButtonsEnabled(false);
    });
    state.mqttClient.on("close", () => {
      state.mqttConnected = false;
      setMqttState("offline", "MQTT OFFLINE");
      setMqttButtonsEnabled(false);
    });
    state.mqttClient.on("error", (error) => {
      elements.controlFeedback.textContent = `MQTT lỗi: ${error.message}`;
    });
    state.mqttClient.on("message", (topic, payload) => {
      if (topic !== mqttConfig.stateTopic) return;
      applyMqttState(payload.toString());
    });
  }

  function applyMqttState(rawValue) {
    let value = rawValue.trim().toLowerCase();
    try {
      const parsed = JSON.parse(rawValue);
      if (typeof parsed.conveyor_on === "boolean") {
        state.reportedConveyor = parsed.conveyor_on;
      } else if (typeof parsed.state === "string") {
        value = parsed.state.toLowerCase();
      }
      if (Number.isFinite(Number(parsed.speed))) setSelectedSpeed(parsed.speed);
    } catch (_) {
    }
    if (["on", "running", "true", "1"].includes(value)) state.reportedConveyor = true;
    if (["off", "stopped", "false", "0"].includes(value)) state.reportedConveyor = false;
    updateConveyorDisplay();
  }

  function publishCommand(command) {
    if (!state.mqttConnected || !state.mqttClient) return;
    const payload = command === "on" ? mqttConfig.payloadOn : mqttConfig.payloadOff;
    state.mqttClient.publish(mqttConfig.controlTopic, String(payload), { qos: 0, retain: false }, (error) => {
      if (error) {
        elements.controlFeedback.textContent = `Không gửi được lệnh: ${error.message}`;
        return;
      }
      state.lastCommand = command;
      if (!mqttConfig.stateTopic) state.reportedConveyor = null;
      updateConveyorDisplay();
      elements.controlFeedback.textContent = `Đã gửi ${String(payload).toUpperCase()} • chờ thiết bị phản hồi`;
    });
  }

  function publishSpeed(speed) {
    if (!state.mqttConnected || !state.mqttClient) return;
    state.mqttClient.publish(mqttConfig.speedTopic, String(speed), { qos: 0, retain: false }, (error) => {
      if (error) {
        elements.controlFeedback.textContent = `Không gửi được tốc độ: ${error.message}`;
        return;
      }
      elements.controlFeedback.textContent = `Đã gửi PWM ${speed} • chờ ESP32 xác nhận`;
    });
  }

  document.querySelectorAll(".source-button").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.source === state.source) return;
      state.source = button.dataset.source;
      document.querySelectorAll(".source-button").forEach((item) => {
        const selected = item === button;
        item.classList.toggle("is-selected", selected);
        item.setAttribute("aria-pressed", String(selected));
      });
      updateCameraSource();
    });
  });

  elements.cameraFeed.addEventListener("load", () => {
    if (state.cameraHidden) return;
    state.cameraLoaded = true;
    elements.cameraFeed.hidden = false;
    elements.cameraPlaceholder.hidden = true;
    elements.cameraLive.hidden = false;
    elements.cameraResolution.textContent = state.source === "yolo" ? "YOLO STREAM" : "CAMERA STREAM";
  });
  elements.cameraFeed.addEventListener("error", () => {
    if (state.cameraHidden) return;
    showCameraPlaceholder("KHÔNG NHẬN ĐƯỢC HÌNH ẢNH", "Kiểm tra trạng thái camera và đường dẫn stream");
  });
  elements.apiInput.value = apiBase;
  elements.apiForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const raw = elements.apiInput.value;
    const next = normalizeBaseUrl(raw);
    if (raw.trim() && !next) {
      elements.apiHint.dataset.state = "error";
      elements.apiHint.textContent = "Link không hợp lệ, ví dụ: https://xxxx.ngrok-free.dev";
      return;
    }
    delete elements.apiHint.dataset.state;
    apiBase = next;
    saveApiBase(next);
    elements.apiInput.value = next;
    elements.apiHint.textContent = next ? `Đang dùng backend: ${next}` : "Đã xóa link backend";
    stopCameraStream();
    updateCameraSource();
    pollStatus();
  });
  if (apiBase) elements.apiHint.textContent = `Đang dùng backend: ${apiBase}`;

  elements.cameraToggle.addEventListener("click", () => setCameraHidden(!state.cameraHidden));
  elements.stopButton.addEventListener("click", () => publishCommand("off"));
  elements.runButton.addEventListener("click", () => publishCommand("on"));
  document.querySelectorAll(".speed-option").forEach((button) => {
    button.addEventListener("click", () => {
      const speed = Number(button.dataset.speed);
      setSelectedSpeed(speed);
      publishSpeed(speed);
    });
  });

  function updateClock() {
    elements.clock.textContent = new Date().toLocaleTimeString("vi-VN", { hour12: false });
  }

  updateClock();
  window.setInterval(updateClock, 1000);
  updateCameraSource();
  pollStatus();
  window.setInterval(pollStatus, Math.max(500, Number(apiConfig.pollIntervalMs) || 1200));
  connectMqtt();
  if (window.lucide && typeof window.lucide.createIcons === "function") window.lucide.createIcons();
})();
