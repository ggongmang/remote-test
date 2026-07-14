"""
server.py - 노트북(교사 PC 역할)에서 실행
클라이언트(학생 PC)로부터 화면 이미지를 받아서 창으로 표시한다.
"""

import socket
import struct
import cv2
import numpy as np

HOST = "0.0.0.0"   # 모든 네트워크 인터페이스에서 연결 수신
PORT = 9999

def recv_exact(sock, n):
    """정확히 n바이트를 받을 때까지 반복해서 읽는다."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("연결이 끊어졌습니다.")
        buf += chunk
    return buf

def main():
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((HOST, PORT))
    server_socket.listen(1)
    print(f"[서버] {PORT}번 포트에서 연결 대기 중...")

    conn, addr = server_socket.accept()
    print(f"[서버] 클라이언트 연결됨: {addr}")

    try:
        while True:
            # 1. 4바이트로 인코딩된 이미지 데이터 길이를 먼저 받는다
            length_bytes = recv_exact(conn, 4)
            (length,) = struct.unpack(">I", length_bytes)

            # 2. 그 길이만큼 실제 이미지 데이터를 받는다
            img_data = recv_exact(conn, length)

            # 3. JPEG 바이트 데이터를 이미지 배열로 디코딩
            img_array = np.frombuffer(img_data, dtype=np.uint8)
            frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)

            if frame is None:
                print("[서버] 프레임 디코딩 실패, 건너뜀")
                continue

            # 4. 화면에 표시
            cv2.imshow("Remote Screen (press Q to quit)", frame)

            # 'q' 키를 누르면 종료
            if cv2.waitKey(1) & 0xFF == ord("q"):
                print("[서버] 종료 요청 받음")
                break

    except ConnectionError as e:
        print(f"[서버] 연결 종료: {e}")
    except KeyboardInterrupt:
        print("[서버] Ctrl+C로 종료")
    finally:
        conn.close()
        server_socket.close()
        cv2.destroyAllWindows()
        print("[서버] 정리 완료, 프로그램 종료")

if __name__ == "__main__":
    main()
