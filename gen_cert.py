"""
gen_cert.py - 노트북(서버)에서 딱 한 번만 실행

TLS 통신에 쓸 자체 서명 인증서(server.crt)와 개인키(server.key)를 생성한다.

⚠️ server.key(개인키)는 절대 GitHub에 올리면 안 됨 - .gitignore에 반드시 추가
   server.crt(공개 인증서)는 커밋해도 됨 - 데스크탑이 git pull로 받아서 사용

실행 전 설치 필요: python -m pip install cryptography
"""

import datetime
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "classroom-server")])

cert = (
    x509.CertificateBuilder()
    .subject_name(name)
    .issuer_name(name)
    .public_key(key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(datetime.datetime.utcnow())
    .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
    .sign(key, hashes.SHA256())
)

with open("server.key", "wb") as f:
    f.write(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))

with open("server.crt", "wb") as f:
    f.write(cert.public_bytes(serialization.Encoding.PEM))

print("생성 완료: server.key (개인키, 노트북에만 보관), server.crt (공개 인증서, 커밋 가능)")
