"""
server.py - 노트북(교사 PC 역할)에서 실행 [M2]
- 영상 채널(9999): 클라이언트로부터 화면을 받아 표시
- 제어 채널(9998): 마우스 클릭/이동, 키보드 입력, 화면 잠금 명령을 클라이언트로 전송

조작법:
- 마우스 이동/좌클릭/우클릭 -> 그대로 학생 PC에 전달됨
- 키보드 입력(영문/숫자 등 인쇄 가능한 문자) -> 학생 PC에 전달됨
- L 키 -> 학생 화면 잠금 토글
- Q 키 -> 종료
"""

import ctypes

# Windows DPI 가상화를 끄고 실제 물리 픽셀 좌표를 그대로 사용하도록 설정
# (배율 설정이 100%가 아닌 경우 좌표가 왜곡되는 문제를 방지)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import socket
import struct
import threading
import queue
import json
import time
import cv2
import numpy as np

VIDEO_PORT = 9999
CONTROL_PORT = 9998

frame_lock = threading.Lock()
latest_frame = None
running = True
control_queue = queue.Queue()
client_ip_holder = {"ip": None}


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("연결이 끊어졌습니다.")
        buf += chunk
    return buf


def video_receiver():
    """영상 채널: 클라이언트 화면을 받아서 latest_frame에 저장"""
    global latest_frame, running
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("0.0.0.0", VIDEO_PORT))
    server_socket.listen(1)
    print(f"[서버-영상] {VIDEO_PORT}번 포트에서 연결 대기 중...")

    conn, addr = server_socket.accept()
    client_ip_holder["ip"] = addr[0]
    print(f"[서버-영상] 클라이언트 연결됨: {addr}")

    try:
        while running:
            length_bytes = recv_exact(conn, 4)
            (length,) = struct.unpack(">I", length_bytes)
            img_data = recv_exact(conn, length)

            img_array = np.frombuffer(img_data, dtype=np.uint8)
            frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
            if frame is None:
                continue

            with frame_lock:
                latest_frame = frame
    except ConnectionError as e:
        print(f"[서버-영상] 연결 종료: {e}")
    finally:
        conn.close()
        server_socket.close()
        running = False


def control_sender():
    """제어 채널: 큐에 쌓인 명령을 클라이언트로 전송"""
    while client_ip_holder["ip"] is None and running:
        time.sleep(0.1)
    if not running:
        return

    client_ip = client_ip_holder["ip"]
    print(f"[서버-제어] {client_ip}:{CONTROL_PORT} 연결 시도 중...")

    control_sock = None
    for _ in range(20):  # 클라이언트가 아직 리스닝 준비 전일 수 있어 재시도
        try:
            control_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            control_sock.connect((client_ip, CONTROL_PORT))
            break
        except (ConnectionRefusedError, OSError):
            control_sock = None
            time.sleep(0.5)

    if control_sock is None:
        print("[서버-제어] 제어 채널 연결 실패 (클라이언트 제어 리스너 확인 필요)")
        return

    print("[서버-제어] 제어 채널 연결됨")
    try:
        while running:
            try:
                cmd = control_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            message = (json.dumps(cmd) + "\n").encode("utf-8")
            control_sock.sendall(message)
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        control_sock.close()


last_move_time = {"t": 0.0}
MOVE_THROTTLE_SEC = 0.05  # 마우스 이동 명령은 최대 초당 20회로 제한


def mouse_callback(event, x, y, flags, param):
    if event == cv2.EVENT_MOUSEMOVE:
        now = time.time()
        if now - last_move_time["t"] < MOVE_THROTTLE_SEC:
            return  # 너무 잦은 이동 이벤트는 건너뛰어 큐가 밀리지 않게 함
        last_move_time["t"] = now
        control_queue.put({"type": "move", "x": x, "y": y})
    elif event == cv2.EVENT_LBUTTONDOWN:
        control_queue.put({"type": "click", "button": "left", "x": x, "y": y})
    elif event == cv2.EVENT_RBUTTONDOWN:
        control_queue.put({"type": "click", "button": "right", "x": x, "y": y})


def main():
    global running

    t_video = threading.Thread(target=video_receiver, daemon=True)
    t_control = threading.Thread(target=control_sender, daemon=True)
    t_video.start()
    t_control.start()

    window_name = "Remote Screen (Mouse: control, L: lock toggle, Q: quit)"
    cv2.namedWindow(window_name)
    cv2.setMouseCallback(window_name, mouse_callback)

    print("[서버] 화면 수신 대기 중...")

    while running:
        with frame_lock:
            frame = latest_frame.copy() if latest_frame is not None else None

        if frame is not None:
            cv2.imshow(window_name, frame)

        key = cv2.waitKey(30) & 0xFF
        if key == ord("q"):
            running = False
            break
        elif key == ord("l"):
            control_queue.put({"type": "lock_toggle"})
            print("[서버] 화면 잠금 토글 명령 전송")
        elif 32 <= key <= 126:  # 인쇄 가능한 ASCII 범위
            control_queue.put({"type": "key", "char": chr(key)})

    cv2.destroyAllWindows()
    print("[서버] 종료")


if __name__ == "__main__":
    main()
