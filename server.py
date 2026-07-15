"""
server.py - 노트북(교사 PC 역할)에서 실행 [M4]

M3 대비 추가된 것:
- 인증: 클라이언트 접속 시 공유 비밀키(AUTH_KEY) 확인, 불일치 시 연결 거부
- 감사 로그: audit_log.csv에 연결/해제/인증실패/선택/잠금 이벤트 기록

조작법 (M3와 동일):
- 숫자 키 1~9: 학생 선택
- ESC: 선택 해제
- L: 선택된 학생 화면 잠금 토글
- Q: 전체 종료
"""

import ctypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import socket
import ssl
import struct
import threading
import queue
import json
import time
import csv
import os
import hmac
import cv2
import numpy as np

# ⚠️ 데스크탑(client.py)의 AUTH_KEY와 반드시 동일해야 함
AUTH_KEY = "classroom-secret-2026"

# TLS 인증서 로드 (gen_cert.py로 미리 생성되어 있어야 함)
ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ssl_context.load_cert_chain(certfile="server.crt", keyfile="server.key")

VIDEO_PORT = 9999
CONTROL_PORT = 9998

THUMB_W, THUMB_H = 320, 180
GRID_COLS = 3
MAX_GRID_HEIGHT = 900  # 학생 수가 많아져도 창이 화면 밖으로 안 나가도록 제한
grid_scale = {"factor": 1.0}

AUDIT_LOG_PATH = "audit_log.csv"
audit_lock = threading.Lock()

METRICS_LOG_PATH = "metrics_log.csv"
metrics_lock = threading.Lock()
METRICS_WINDOW = 30  # 이 프레임 수마다 한 번씩 통계 계산

running = True
clients = {}
clients_lock = threading.Lock()
next_id_holder = {"n": 1}
selected_id = {"id": None}

last_move_time = {"t": 0.0}
MOVE_THROTTLE_SEC = 0.05


def log_audit(cid, action, detail=""):
    """중요 이벤트를 audit_log.csv에 기록 (타임스탬프, 학생ID, 행동, 상세)"""
    with audit_lock:
        file_exists = os.path.isfile(AUDIT_LOG_PATH)
        with open(AUDIT_LOG_PATH, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["timestamp", "student_id", "action", "detail"])
            writer.writerow([
                time.strftime("%Y-%m-%d %H:%M:%S"),
                cid if cid is not None else "-",
                action,
                detail,
            ])


def log_metrics(cid, avg_latency_ms, fps, kbps, concurrent_clients):
    with metrics_lock:
        file_exists = os.path.isfile(METRICS_LOG_PATH)
        with open(METRICS_LOG_PATH, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["timestamp", "student_id", "avg_latency_ms", "fps", "kbps", "concurrent_clients"])
            writer.writerow([
                time.strftime("%Y-%m-%d %H:%M:%S"),
                cid,
                f"{avg_latency_ms:.2f}",
                f"{fps:.2f}",
                f"{kbps:.2f}",
                concurrent_clients,
            ])


class ConnReader:
    """TCP는 스트림이라 메시지 경계가 보장되지 않으므로,
    줄바꿈 기준(인증 메시지)과 길이 헤더 기준(영상 프레임)을 모두
    다룰 수 있도록 버퍼를 직접 관리하는 헬퍼."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("연결이 끊어졌습니다.")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def read_line(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("연결이 끊어졌습니다.")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line


def control_reader(cid, control_sock):
    """제어 채널에서 pong 응답을 읽어 RTT 기반 지연시간을 계산"""
    buffer = b""
    try:
        while running and cid in clients:
            data = control_sock.recv(4096)
            if not data:
                break
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "pong" and cid in clients:
                    rtt_ms = (time.time() - msg["t"]) * 1000
                    clients[cid]["latency_ms"] = rtt_ms / 2  # RTT의 절반을 편도 지연 근사치로 사용
    except (OSError, KeyError):
        pass


def control_sender(cid, client_ip):
    control_sock = None
    for _ in range(20):
        if cid not in clients or not running:
            return
        try:
            control_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            control_sock.connect((client_ip, CONTROL_PORT))
            break
        except (ConnectionRefusedError, OSError):
            control_sock = None
            time.sleep(0.5)

    if control_sock is None:
        print(f"[서버] 학생 #{cid} 제어 채널 연결 실패")
        return

    print(f"[서버] 학생 #{cid} 제어 채널 연결됨")
    threading.Thread(target=control_reader, args=(cid, control_sock), daemon=True).start()

    last_ping = 0.0
    try:
        while running and cid in clients:
            try:
                cmd = clients[cid]["control_queue"].get(timeout=0.5)
                message = (json.dumps(cmd) + "\n").encode("utf-8")
                control_sock.sendall(message)
            except queue.Empty:
                pass

            now = time.time()
            if now - last_ping >= 3.0:
                last_ping = now
                ping_msg = (json.dumps({"type": "ping", "t": now}) + "\n").encode("utf-8")
                control_sock.sendall(ping_msg)
    except (BrokenPipeError, ConnectionResetError, KeyError):
        pass
    finally:
        control_sock.close()


def handle_client(conn, addr):
    """접속 -> 인증 -> (성공 시) 영상 수신 루프까지 한 스레드가 전담"""
    reader = ConnReader(conn)

    # 1. 인증 절차
    try:
        auth_line = reader.read_line()
        auth_msg = json.loads(auth_line.decode("utf-8"))
    except Exception:
        conn.close()
        print(f"[서버] 인증 메시지 형식 오류, 연결 거부: {addr}")
        log_audit(None, "AUTH_FAIL", f"IP={addr[0]} (형식 오류)")
        return

    if not hmac.compare_digest(str(auth_msg.get("token", "")), AUTH_KEY):
        try:
            conn.sendall((json.dumps({"status": "rejected"}) + "\n").encode())
        except OSError:
            pass
        conn.close()
        print(f"[서버] 인증 실패, 연결 거부: {addr}")
        log_audit(None, "AUTH_FAIL", f"IP={addr[0]}")
        return

    try:
        conn.sendall((json.dumps({"status": "ok"}) + "\n").encode())
    except OSError:
        conn.close()
        return

    # 2. 인증 성공 -> 클라이언트 등록
    with clients_lock:
        cid = next_id_holder["n"]
        next_id_holder["n"] += 1
        clients[cid] = {
            "frame": None,
            "frame_lock": threading.Lock(),
            "addr": addr[0],
            "control_queue": queue.Queue(),
            "latency_ms": None,
        }

    print(f"[서버] 학생 #{cid} 인증 성공, 연결됨: {addr}")
    log_audit(cid, "CONNECT", f"IP={addr[0]}")

    threading.Thread(target=control_sender, args=(cid, addr[0]), daemon=True).start()

    # 3. 영상 수신 루프 (+ FPS/대역폭 측정, 지연시간은 별도 RTT 핑퐁으로 측정됨)
    stats = {"bytes": 0, "frame_count": 0, "window_start": time.time()}
    try:
        while running and cid in clients:
            length_bytes = reader.read_exact(4)
            (length,) = struct.unpack(">I", length_bytes)
            img_data = reader.read_exact(length)

            img_array = np.frombuffer(img_data, dtype=np.uint8)
            frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
            if frame is None:
                continue

            with clients[cid]["frame_lock"]:
                clients[cid]["frame"] = frame

            stats["bytes"] += length
            stats["frame_count"] += 1

            if stats["frame_count"] >= METRICS_WINDOW:
                elapsed = time.time() - stats["window_start"]
                fps = stats["frame_count"] / elapsed if elapsed > 0 else 0
                kbps = (stats["bytes"] / 1024) / elapsed if elapsed > 0 else 0
                latency = clients[cid].get("latency_ms")
                with clients_lock:
                    concurrent = len(clients)
                log_metrics(cid, latency if latency is not None else -1, fps, kbps, concurrent)
                latency_str = f"{latency:.1f}ms" if latency is not None else "측정중..."
                print(f"[서버] 학생 #{cid} 측정: 지연(RTT/2) {latency_str}, "
                      f"FPS {fps:.1f}, 대역폭 {kbps:.1f}KB/s, 동시접속 {concurrent}명")
                stats = {"bytes": 0, "frame_count": 0, "window_start": time.time()}
    except (ConnectionError, KeyError):
        pass
    finally:
        conn.close()
        with clients_lock:
            clients.pop(cid, None)
        if selected_id["id"] == cid:
            selected_id["id"] = None
        print(f"[서버] 학생 #{cid} 연결 끊김")
        log_audit(cid, "DISCONNECT")


def video_accept_loop():
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("0.0.0.0", VIDEO_PORT))
    server_socket.listen(9)
    print(f"[서버] {VIDEO_PORT}번 포트에서 학생 연결 대기 중...")

    while running:
        try:
            conn, addr = server_socket.accept()
        except OSError:
            break

        try:
            tls_conn = ssl_context.wrap_socket(conn, server_side=True)
        except ssl.SSLError as e:
            print(f"[서버] TLS 핸드셰이크 실패, 연결 거부: {addr} - {e}")
            conn.close()
            log_audit(None, "TLS_FAIL", f"IP={addr[0]}")
            continue

        threading.Thread(target=handle_client, args=(tls_conn, addr), daemon=True).start()

    server_socket.close()


def build_grid_image():
    with clients_lock:
        ids = sorted(clients.keys())

    if not ids:
        blank = np.zeros((THUMB_H, THUMB_W, 3), dtype=np.uint8)
        cv2.putText(blank, "학생 연결 대기중...", (10, THUMB_H // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        return blank

    thumbs = []
    for cid in ids:
        with clients[cid]["frame_lock"]:
            frame = clients[cid]["frame"]
        if frame is None:
            thumb = np.zeros((THUMB_H, THUMB_W, 3), dtype=np.uint8)
        else:
            thumb = cv2.resize(frame, (THUMB_W, THUMB_H))

        border_color = (0, 255, 0) if selected_id["id"] == cid else (80, 80, 80)
        cv2.rectangle(thumb, (0, 0), (THUMB_W - 1, THUMB_H - 1), border_color, 3)
        cv2.putText(thumb, f"#{cid}", (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        thumbs.append(thumb)

    rows = []
    for i in range(0, len(thumbs), GRID_COLS):
        row_thumbs = thumbs[i:i + GRID_COLS]
        while len(row_thumbs) < GRID_COLS:
            row_thumbs.append(np.zeros((THUMB_H, THUMB_W, 3), dtype=np.uint8))
        rows.append(np.hstack(row_thumbs))

    big_grid = np.vstack(rows)
    h, w = big_grid.shape[:2]
    if h > MAX_GRID_HEIGHT:
        factor = MAX_GRID_HEIGHT / h
        big_grid = cv2.resize(big_grid, (int(w * factor), int(h * factor)))
        grid_scale["factor"] = factor
    else:
        grid_scale["factor"] = 1.0

    return big_grid


def grid_mouse_callback(event, x, y, flags, param):
    if event != cv2.EVENT_LBUTTONDOWN:
        return
    factor = grid_scale["factor"] if grid_scale["factor"] > 0 else 1.0
    orig_x = x / factor
    orig_y = y / factor
    col = int(orig_x // THUMB_W)
    row = int(orig_y // THUMB_H)
    idx = row * GRID_COLS + col
    with clients_lock:
        ids = sorted(clients.keys())
    if 0 <= idx < len(ids):
        candidate = ids[idx]
        selected_id["id"] = candidate
        print(f"[서버] 학생 #{candidate} 선택됨 (클릭)")
        log_audit(candidate, "SELECT", "클릭")


def control_mouse_callback(event, x, y, flags, param):
    cid = selected_id["id"]
    if cid is None or cid not in clients:
        return

    if event == cv2.EVENT_MOUSEMOVE:
        now = time.time()
        if now - last_move_time["t"] < MOVE_THROTTLE_SEC:
            return
        last_move_time["t"] = now
        clients[cid]["control_queue"].put({"type": "move", "x": x, "y": y})
    elif event == cv2.EVENT_LBUTTONDOWN:
        clients[cid]["control_queue"].put({"type": "click", "button": "left", "x": x, "y": y})
    elif event == cv2.EVENT_RBUTTONDOWN:
        clients[cid]["control_queue"].put({"type": "click", "button": "right", "x": x, "y": y})


def main():
    global running

    threading.Thread(target=video_accept_loop, daemon=True).start()

    grid_window = "Classroom Monitor (1-9: select student, Q: quit)"
    control_window = "Control"
    cv2.namedWindow(grid_window)
    cv2.setMouseCallback(grid_window, grid_mouse_callback)
    control_window_open = False

    while running:
        grid_img = build_grid_image()
        cv2.imshow(grid_window, grid_img)

        cid = selected_id["id"]
        if cid is not None and cid in clients:
            if not control_window_open:
                cv2.namedWindow(control_window)
                cv2.setMouseCallback(control_window, control_mouse_callback)
                control_window_open = True
            with clients[cid]["frame_lock"]:
                frame = clients[cid]["frame"]
            if frame is not None:
                cv2.imshow(control_window, frame)
        else:
            if control_window_open:
                cv2.destroyWindow(control_window)
                control_window_open = False

        key = cv2.waitKey(30) & 0xFF

        if key == ord("q"):
            running = False
            break
        elif key == 27:  # ESC
            selected_id["id"] = None
        elif ord("1") <= key <= ord("9"):
            candidate = key - ord("0")
            with clients_lock:
                if candidate in clients:
                    selected_id["id"] = candidate
                    print(f"[서버] 학생 #{candidate} 선택됨")
                    log_audit(candidate, "SELECT")
        elif key == ord("l"):
            cid = selected_id["id"]
            if cid is not None and cid in clients:
                clients[cid]["control_queue"].put({"type": "lock_toggle"})
                print(f"[서버] 학생 #{cid} 잠금 토글 명령 전송")
                log_audit(cid, "LOCK_TOGGLE")
        elif 32 <= key <= 126:
            cid = selected_id["id"]
            if cid is not None and cid in clients:
                clients[cid]["control_queue"].put({"type": "key", "char": chr(key)})

    cv2.destroyAllWindows()
    print("[서버] 종료")


if __name__ == "__main__":
    main()
