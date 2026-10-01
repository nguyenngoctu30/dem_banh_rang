# API theo dõi bánh răng

Các API chạy cùng ứng dụng trong `banhrang_tracking.py`, mặc định lắng nghe tại cổng `8000` trên mọi giao diện mạng (`0.0.0.0`). 

## Khởi động

Mở PowerShell tại thư mục dự án và chạy:

```powershell
python .\banhrang_tracking.py
```

Khi ứng dụng khởi động thành công, terminal hiển thị:

```text
[API] Listening on http://0.0.0.0:8000
```

Trên chính máy chạy ứng dụng, dùng `http://localhost:8000`. Máy khác trong mạng LAN có thể dùng `http://<IP-máy-chạy-app>:8000`.

## Danh sách endpoint

| Method | Đường dẫn | Nội dung |
|---|---|---|
| `GET` | `/api/status` | Số lượng sản phẩm/lỗi, trạng thái camera và FPS dạng JSON |
| `GET` | `/api/camera/stream` | Video trực tiếp từ camera, định dạng MJPEG |
| `GET` | `/api/camera/yolo` | Video đã được YOLO vẽ kết quả, định dạng MJPEG |

### Lấy số lượng: `/api/status`

Ví dụ gọi từ PowerShell:

```powershell
Invoke-RestMethod http://localhost:8000/api/status
```

Ví dụ response:

```json
{
  "self_total": 12,
  "self_total_err": 3,
  "self_e0_count": 9,
  "self_e1_count": 1,
  "self_e2_count": 2,
  "self_e3_count": 0,
  "camera_running": true,
  "yolo_running": true,
  "cam_fps": 30.0,
  "detect_fps": 12.5
}
```

Ý nghĩa các trường:

| Trường | Ý nghĩa |
|---|---|
| `self_total` | Tổng số bánh răng đã đi qua vạch đếm |
| `self_total_err` | Tổng số bánh răng lỗi |
| `self_e0_count` | Bình thường |
| `self_e1_count` | Thiếu vòng bi |
| `self_e2_count` | Sứt mẻ |
| `self_e3_count` | Bẩn/ố |
| `camera_running` | Thread camera đang chạy hay không |
| `yolo_running` | Thread YOLO đang chạy hay không |
| `cam_fps` | FPS camera gần đây |
| `detect_fps` | FPS xử lý YOLO gần đây |

Các số đếm bắt đầu từ `0` khi khởi động chương trình và được đưa về `0` bằng chức năng Reset trong giao diện. API chỉ đọc số liệu, không có endpoint điều khiển/reset.

### Camera trực tiếp

Mở URL này bằng trình duyệt hoặc dùng làm nguồn ảnh trong giao diện web:

```text
http://localhost:8000/api/camera/stream
```

Ví dụ HTML:

```html
<img src="http://localhost:8000/api/camera/stream" alt="Camera">
```

### Camera có YOLO

```text
http://localhost:8000/api/camera/yolo
```

Ví dụ HTML:

```html
<img src="http://localhost:8000/api/camera/yolo" alt="Camera YOLO">
```

Hai URL video trả luồng MJPEG, không phải JSON. Nếu camera chưa bật hoặc chưa có frame, server trả HTTP `503` với nội dung lỗi JSON. Đóng kết nối stream bằng cách đóng trang/tab hoặc dừng request từ client.

## Dùng Cloudflare Tunnel

Để thử nhanh, cài `cloudflared`, khởi động ứng dụng trước, rồi chạy:

```powershell
cloudflared tunnel --url http://localhost:8000
```

Cloudflare sẽ in ra một URL công khai dạng `https://....trycloudflare.com`. Thay `localhost:8000` bằng URL đó khi gọi API, ví dụ:

```text
https://....trycloudflare.com/api/status
https://....trycloudflare.com/api/camera/stream
https://....trycloudflare.com/api/camera/yolo
```

Quick Tunnel thường cấp URL tạm thời; khi cần địa chỉ ổn định, hãy cấu hình tunnel gắn với domain trong Cloudflare.

## MQTT điều khiển băng tải bằng ESP32

Web và ESP32 dùng chung broker/topic/payload sau:

| Cấu hình | Giá trị |
|---|---|
| Broker | `broker.hivemq.com` |
| Web transport | Secure WebSocket, port `8884` |
| ESP32 transport | MQTT TCP, port `1883` |
| Topic điều khiển | `banhrang/conveyor/control` |
| Payload chạy | `on` |
| Payload dừng | `off` |

Sketch nằm tại `esp32_conveyor_mqtt.ino`. Mở file trong Arduino IDE, cài thư viện **PubSubClient** (Nick O'Leary), rồi điền `WIFI_SSID` và `WIFI_PASSWORD` ở đầu file trước khi nạp lên ESP32. Serial Monitor dùng baud `115200`; khi kết nối thành công sẽ hiện `Subscribed to banhrang/conveyor/control`.

Khi nhận `on`, chương trình đặt GPIO4 ở mức HIGH. Khi nhận `off`, GPIO4 về LOW. GPIO4 cũng được đặt LOW lúc khởi động và khi Wi-Fi/MQTT mất kết nối. Payload khác `on`/`off` sẽ bị bỏ qua.

GPIO4 chỉ là tín hiệu logic 3.3V

## Lưu ý bảo mật
