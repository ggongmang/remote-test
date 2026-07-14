"""
server.py - 노트북(교사 PC 역할)에서 실행 [M3]

구조:
- 영상 채널(9999): 여러 클라이언트를 동시에 accept, 각 클라이언트마다 수신 스레드 생성
- 제어 채널(9998): 클라이언트별로 별도 연결, 선택된 학생에게만 명령 전송

화면:
- "Classroom Monitor" 창: 전체 학생 화면 썸네일 그리드 (읽기 전용)
- "Control" 창: 숫자 키(1~9)로 선택한 학생 화면을 크게 표시, 마우스/키보드/잠금 제어 가능

조작법:
- 숫자 키 1~9: 해당 번호의 학생 선택 (Control 창 열림)
- ESC: 선택 해제 (Control 창 닫고 그리드로 복귀)
- L: 선택된 학생 화면 잠금 토글
- Q: 전체 종료

한계: 현재 버전은 학생 수가 9명을 넘으면 숫자 키로 선택할 수 없음.
      실제 30명 규모 교실에서는 클릭 기반 선택으로 개선 필요 (다음 단계 과제).
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
import struct
import threading
import queue
import json
import time
import cv2
import numpy as np

VIDEO_PORT = 9999
CONTROL_PORT = 9998

THUMB_W, THUMB_H = 320, 180  # 그리드 썸네일 크기
GRID_COLS = 3

running = True
clients = {}          # cid -> {"frame":..., "frame_lock":..., "addr":..., "control_queue":...}
clients_lock = threading.Lock()
next_id_holder = {"n": 1}

selected_id = {"id": None}

last_move_time = {"t": 0.0}
MOVE_THROTTLE_SEC = 0.05


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("연결이 끊어졌습니다.")
        buf += chunk
    return buf


def video_receiver(cid, conn):
    try:
        while running and cid in clients:
            length_bytes = recv_exact(conn, 4)
            (length,) = struct.unpack(">I", length_bytes)
            img_data = recv_exact(conn, length)

            img_array = np.frombuffer(img_data, dtype=np.uint8)
            frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
            if frame is None:
                continue

            with clients[cid]["frame_lock"]:
                clients[cid]["frame"] = frame
    except (ConnectionError, KeyError):
        pass
    finally:
        conn.close()
        with clients_lock:
            clients.pop(cid, None)
        if selected_id["id"] == cid:
            selected_id["id"] = None
        print(f"[서버] 학생 #{cid} 연결 끊김")


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
    try:
        while running and cid in clients:
            try:
                cmd = clients[cid]["control_queue"].get(timeout=0.5)
            except queue.Empty:
                continue
            message = (json.dumps(cmd) + "\n").encode("utf-8")
            control_sock.sendall(message)
    except (BrokenPipeError, ConnectionResetError, KeyError):
        pass
    finally:
        control_sock.close()


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

        with clients_lock:
            cid = next_id_holder["n"]
            next_id_holder["n"] += 1
            clients[cid] = {
                "frame": None,
                "frame_lock": threading.Lock(),
                "addr": addr[0],
                "control_queue": queue.Queue(),
            }

        print(f"[서버] 학생 #{cid} 연결됨: {addr}")
        threading.Thread(target=video_receiver, args=(cid, conn), daemon=True).start()
        threading.Thread(target=control_sender, args=(cid, addr[0]), daemon=True).start()

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

    return np.vstack(rows)


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
    # Control 창은 학생 선택 시점에 생성하므로 콜백도 그때 등록

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
        elif key == ord("l"):
            cid = selected_id["id"]
            if cid is not None and cid in clients:
                clients[cid]["control_queue"].put({"type": "lock_toggle"})
                print(f"[서버] 학생 #{cid} 잠금 토글 명령 전송")
        elif 32 <= key <= 126:
            cid = selected_id["id"]
            if cid is not None and cid in clients:
                clients[cid]["control_queue"].put({"type": "key", "char": chr(key)})

    cv2.destroyAllWindows()
    print("[서버] 종료")


if __name__ == "__main__":
    main()
