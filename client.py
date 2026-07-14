"""
client.py - 데스크탑(학생 PC 역할)에서 실행 [M2]
- 영상 채널(9999): 화면을 캡처해서 서버로 전송
- 제어 채널(9998): 서버로부터 마우스/키보드/잠금 명령을 받아 실행

주의: 화면 잠금은 실제로 키보드/마우스 입력을 막는 것이 아니라,
전체화면 검은 오버레이 창을 띄워 화면을 가리는 방식입니다.
(OS 레벨 입력 차단은 이번 단계 범위 밖 - 알려진 한계로 문서화)
"""

import ctypes

# Windows DPI 가상화를 끄고 실제 물리 픽셀 좌표를 그대로 사용하도록 설정
# (mss 캡처 좌표와 pyautogui 제어 좌표가 어긋나는 문제를 방지)
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
import json
import time
import tkinter as tk

import mss
import cv2
import numpy as np
import pyautogui

# 여기를 노트북(서버)의 IP 주소로 바꿔주세요
SERVER_IP = "172.30.1.53"
VIDEO_PORT = 9999
CONTROL_PORT = 9998

JPEG_QUALITY = 60
TARGET_FPS = 10

pyautogui.FAILSAFE = False  # 원격 제어 특성상 모서리 이동 예외를 끔

running = True
lock_state = {"locked": False}
overlay_root = None


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def video_sender():
    """영상 채널: 화면을 캡처해서 서버로 전송"""
    global running
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    print(f"[클라이언트-영상] {SERVER_IP}:{VIDEO_PORT} 연결 시도 중...")
    try:
        client_socket.connect((SERVER_IP, VIDEO_PORT))
    except OSError as e:
        print(f"[클라이언트-영상] 연결 실패: {e}")
        running = False
        return
    print("[클라이언트-영상] 서버에 연결됨")

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
    except (ConnectionError, ConnectionResetError, BrokenPipeError) as e:
        print(f"[클라이언트-영상] 연결 종료: {e}")
    finally:
        client_socket.close()
        running = False


def handle_command(cmd):
    ctype = cmd.get("type")

    # 잠금 상태에서는 잠금 해제 명령 외에는 무시 (교사만 해제 가능하게)
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
        # Windows API로 실제 마우스/키보드 입력 차단 (Ctrl+Alt+Del은 OS가 예외 처리)
        result = ctypes.windll.user32.BlockInput(lock_state["locked"])
        if result == 0:
            print("[클라이언트] 경고: BlockInput 실패 - 관리자 권한으로 실행했는지 확인하세요")
        print(f"[클라이언트] 잠금 상태 변경: {lock_state['locked']}")


def control_receiver():
    """제어 채널: 서버로부터 명령을 받아 실행"""
    global running
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
                handle_command(cmd)
    except (ConnectionResetError, OSError):
        pass
    finally:
        conn.close()
        server_socket.close()


def main():
    global overlay_root

    if not is_admin():
        print("[클라이언트] 경고: 관리자 권한이 아닙니다. BlockInput(입력 차단)이 동작하지 않을 수 있습니다.")
        print("[클라이언트] PowerShell을 관리자 권한으로 다시 실행한 뒤 시도하세요.")

    t_video = threading.Thread(target=video_sender, daemon=True)
    t_control = threading.Thread(target=control_receiver, daemon=True)
    t_video.start()
    t_control.start()

    # 잠금 오버레이 창 준비 (평소엔 숨김 상태)
    overlay_root = tk.Tk()
    overlay_root.withdraw()
    overlay_root.attributes("-fullscreen", True)
    overlay_root.attributes("-topmost", True)
    overlay_root.configure(bg="black")
    label = tk.Label(
        overlay_root,
        text="선생님이 화면을 잠갔습니다",
        fg="white",
        bg="black",
        font=("맑은 고딕", 30),
    )
    label.pack(expand=True)

    last_state = False

    def poll():
        nonlocal last_state
        if not running:
            overlay_root.destroy()
            return
        if lock_state["locked"] != last_state:
            if lock_state["locked"]:
                overlay_root.deiconify()
            else:
                overlay_root.withdraw()
            last_state = lock_state["locked"]
        overlay_root.after(200, poll)

    overlay_root.after(200, poll)
    try:
        overlay_root.mainloop()
    finally:
        # 안전장치: 프로그램이 어떤 이유로든 종료될 때 입력 차단이 풀린 상태로 남지 않도록 강제 해제
        ctypes.windll.user32.BlockInput(False)
        print("[클라이언트] 종료 (입력 차단 강제 해제됨)")


if __name__ == "__main__":
    main()
