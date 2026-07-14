"""
client.py - 데스크탑(학생 PC 역할)에서 실행
자신의 화면을 캡처해서 서버(노트북)로 전송한다.
"""

import socket
import struct
import time
import mss
import cv2
import numpy as np

# ⚠️ 여기를 노트북(서버)의 IP 주소로 바꿔주세요
SERVER_IP = "172.30.1.53"
SERVER_PORT = 9999

JPEG_QUALITY = 60   # 0~100, 낮을수록 용량 작고 화질 낮음. 처음엔 60으로 테스트
TARGET_FPS = 10     # 초당 전송 프레임 수 (M1 단계는 낮게 시작)

def main():
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    print(f"[클라이언트] {SERVER_IP}:{SERVER_PORT} 에 연결 시도 중...")
    client_socket.connect((SERVER_IP, SERVER_PORT))
    print("[클라이언트] 서버에 연결됨")

    frame_interval = 1.0 / TARGET_FPS
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]

    try:
        with mss.mss() as sct:
            monitor = sct.monitors[1]  # 기본 모니터 전체 (0번은 전체 가상화면, 1번이 보통 주모니터)

            while True:
                start_time = time.time()

                # 1. 화면 캡처
                screenshot = sct.grab(monitor)
                frame = np.array(screenshot)  # BGRA 형태
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

                # 2. JPEG로 압축 인코딩
                success, encoded = cv2.imencode(".jpg", frame, encode_params)
                if not success:
                    print("[클라이언트] 인코딩 실패, 건너뜀")
                    continue

                img_bytes = encoded.tobytes()

                # 3. 길이(4바이트) + 데이터 순서로 전송
                length_header = struct.pack(">I", len(img_bytes))
                client_socket.sendall(length_header)
                client_socket.sendall(img_bytes)

                # 4. 목표 FPS에 맞춰 대기
                elapsed = time.time() - start_time
                sleep_time = frame_interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

    except (ConnectionError, ConnectionResetError, BrokenPipeError) as e:
        print(f"[클라이언트] 연결 종료: {e}")
    except KeyboardInterrupt:
        print("[클라이언트] Ctrl+C로 종료")
    finally:
        client_socket.close()
        print("[클라이언트] 정리 완료, 프로그램 종료")

if __name__ == "__main__":
    main()
