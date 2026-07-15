"""
client.py - 데스크탑(학생 PC 역할)에서 실행 [M4]

M2/M3 대비 추가된 것:
- 인증: 서버 접속 시 공유 비밀키(AUTH_KEY)를 전송, 서버가 거부하면 즉시 종료
- 상시 동의 표시: 서버에 연결되어 있는 동안 화면 상단에 항상
  "선생님이 이 화면을 보고 있습니다" 배너를 표시 (잠금 여부와 무관하게 항상 뜸)

주의: 화면 잠금은 실제 입력을 BlockInput()으로 차단하지만,
동의 표시 배너는 시각적 표시일 뿐 사용자가 다른 창으로 가릴 수 있음 (알려진 한계)
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
import json
import time
import tkinter as tk

import mss
import cv2
import numpy as np
import pyautogui

# ⚠️ 노트북(server.py)의 AUTH_KEY와 반드시 동일해야 함
AUTH_KEY = "classroom-secret-2026"

# TLS: server.crt(공개 인증서, git으로 받은 것)로 서버 신원을 검증 (TOFU 방식)
tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
tls_context.check_hostname = False  # IP로 접속하므로 호스트명 검사는 끔
tls_context.load_verify_locations(cafile="server.crt")

# ⚠️ 여기를 노트북(서버)의 IP 주소로 바꿔주세요
SERVER_IP = "172.30.1.53"
VIDEO_PORT = 9999
CONTROL_PORT = 9998

JPEG_QUALITY = 60
TARGET_FPS = 10

pyautogui.FAILSAFE = False

running = True
lock_state = {"locked": False}
connection_state = {"connected": False}
overlay_root = None
banner_root = None


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def recv_line(sock):
    """개행 문자가 나올 때까지 받아서 한 줄을 반환 (인증 응답 수신용)"""
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("서버 응답 없음")
        buf += chunk
    line, _ = buf.split(b"\n", 1)
    return line


def video_sender():
    global running
    while running:
        raw_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        print(f"[클라이언트-영상] {SERVER_IP}:{VIDEO_PORT} 연결 시도 중... (TLS)")
        try:
            client_socket = tls_context.wrap_socket(raw_socket, server_hostname="classroom-server")
            client_socket.connect((SERVER_IP, VIDEO_PORT))
        except ssl.SSLCertVerificationError as e:
            print(f"[클라이언트-영상] 서버 인증서 검증 실패! 접속 대상이 진짜 우리 서버가 맞는지 의심됨: {e}")
            running = False
            return
        except OSError as e:
            print(f"[클라이언트-영상] 연결 실패: {e} - 5초 후 재시도")
            time.sleep(5)
            continue

        # 인증 절차: 토큰 전송 -> 서버 응답 확인
        try:
            client_socket.sendall((json.dumps({"token": AUTH_KEY}) + "\n").encode("utf-8"))
            resp_line = recv_line(client_socket)
            resp = json.loads(resp_line.decode("utf-8"))
        except (ConnectionError, json.JSONDecodeError, OSError) as e:
            print(f"[클라이언트-영상] 인증 통신 오류: {e} - 5초 후 재시도")
            client_socket.close()
            time.sleep(5)
            continue

        if resp.get("status") != "ok":
            print("[클라이언트-영상] 인증 실패 - 서버가 접속을 거부했습니다. AUTH_KEY를 확인하세요. (재시도 안 함)")
            client_socket.close()
            running = False
            return

        print("[클라이언트-영상] 인증 성공, 화면 전송 시작")
        connection_state["connected"] = True

        frame_interval = 1.0 / TARGET_FPS
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]

        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                while running:
                    start_time = time.time()

                    screenshot = sct.grab(monitor)
                    frame = np.array(screenshot)
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

                    success, encoded = cv2.imencode(".jpg", frame, encode_params)
                    if not success:
                        continue

                    img_bytes = encoded.tobytes()
                    length_header = struct.pack(">I", len(img_bytes))
                    client_socket.sendall(length_header)
                    client_socket.sendall(img_bytes)

                    elapsed = time.time() - start_time
                    sleep_time = frame_interval - elapsed
                    if sleep_time > 0:
                        time.sleep(sleep_time)
        except (ConnectionError, ConnectionResetError, BrokenPipeError, OSError) as e:
            print(f"[클라이언트-영상] 연결 끊김: {e}")
        finally:
            client_socket.close()
            connection_state["connected"] = False

        if running:
            print("[클라이언트-영상] 5초 후 재연결 시도...")
            time.sleep(5)


def handle_command(cmd, conn):
    ctype = cmd.get("type")

    if ctype == "ping":
        try:
            conn.sendall((json.dumps({"type": "pong", "t": cmd["t"]}) + "\n").encode("utf-8"))
        except OSError:
            pass
        return

    if lock_state["locked"] and ctype != "lock_toggle":
        return

    if ctype == "move":
        pyautogui.moveTo(cmd["x"], cmd["y"])
    elif ctype == "click":
        pyautogui.click(x=cmd["x"], y=cmd["y"], button=cmd.get("button", "left"))
    elif ctype == "key":
        pyautogui.press(cmd["char"])
    elif ctype == "lock_toggle":
        lock_state["locked"] = not lock_state["locked"]
        result = ctypes.windll.user32.BlockInput(lock_state["locked"])
        if result == 0:
            print("[클라이언트] 경고: BlockInput 실패 - 관리자 권한으로 실행했는지 확인하세요")
        print(f"[클라이언트] 잠금 상태 변경: {lock_state['locked']}")


def control_receiver():
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("0.0.0.0", CONTROL_PORT))
    server_socket.listen(1)
    print(f"[클라이언트-제어] {CONTROL_PORT}번 포트에서 대기 중...")

    conn, addr = server_socket.accept()
    print(f"[클라이언트-제어] 제어 채널 연결됨: {addr}")

    buffer = b""
    try:
        while running:
            data = conn.recv(4096)
            if not data:
                break
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line:
                    continue
                try:
                    cmd = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                handle_command(cmd, conn)
    except (ConnectionResetError, OSError):
        pass
    finally:
        conn.close()
        server_socket.close()


def main():
    global overlay_root, banner_root

    if not is_admin():
        print("[클라이언트] 경고: 관리자 권한이 아닙니다. BlockInput(입력 차단)이 동작하지 않을 수 있습니다.")
        print("[클라이언트] PowerShell을 관리자 권한으로 다시 실행한 뒤 시도하세요.")

    t_video = threading.Thread(target=video_sender, daemon=True)
    t_control = threading.Thread(target=control_receiver, daemon=True)
    t_video.start()
    t_control.start()

    # --- 잠금 오버레이 창 (전체화면, 평소엔 숨김) ---
    overlay_root = tk.Tk()
    overlay_root.withdraw()
    overlay_root.attributes("-fullscreen", True)
    overlay_root.attributes("-topmost", True)
    overlay_root.configure(bg="black")
    tk.Label(
        overlay_root, text="선생님이 화면을 잠갔습니다",
        fg="white", bg="black", font=("맑은 고딕", 30),
    ).pack(expand=True)

    # --- 상시 동의 표시 배너 (작은 창, 연결되어 있는 동안 항상 표시) ---
    banner_root = tk.Toplevel(overlay_root)
    banner_root.withdraw()
    banner_root.overrideredirect(True)  # 제목표시줄/닫기버튼 없앰
    banner_root.attributes("-topmost", True)
    screen_w = banner_root.winfo_screenwidth()
    banner_w, banner_h = 420, 32
    banner_root.geometry(f"{banner_w}x{banner_h}+{(screen_w - banner_w) // 2}+0")
    banner_root.configure(bg="#B00020")
    tk.Label(
        banner_root, text="🔴 선생님이 이 화면을 보고 있습니다",
        fg="white", bg="#B00020", font=("맑은 고딕", 11, "bold"),
    ).pack(expand=True, fill="both")

    last_lock_state = False
    last_conn_state = False

    def poll():
        nonlocal last_lock_state, last_conn_state
        if not running:
            overlay_root.destroy()
            return

        if lock_state["locked"] != last_lock_state:
            if lock_state["locked"]:
                overlay_root.deiconify()
            else:
                overlay_root.withdraw()
            last_lock_state = lock_state["locked"]

        if connection_state["connected"] != last_conn_state:
            if connection_state["connected"]:
                banner_root.deiconify()
            else:
                banner_root.withdraw()
            last_conn_state = connection_state["connected"]

        overlay_root.after(200, poll)

    overlay_root.after(200, poll)
    try:
        overlay_root.mainloop()
    finally:
        ctypes.windll.user32.BlockInput(False)
        print("[클라이언트] 종료 (입력 차단 강제 해제됨)")


if __name__ == "__main__":
    main()
