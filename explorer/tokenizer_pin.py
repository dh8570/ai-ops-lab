"""토크나이저 파일의 출처를 한 곳에 고정한다.
Docker 빌드(fetch_tokenizer.py), 서버(/api/config), 검사 스크립트가 같이 쓴다."""

REPO = "Qwen/Qwen2.5-7B-Instruct"
# Hugging Face 커밋 해시(2026-10-28 조회). 브랜치 이름 대신 이 해시로 받아 내용이 바뀌지 않게 한다.
REVISION = "a09a35458c702b33eeacc393d103063234e8bc28"
# 위 해시에서 받은 tokenizer.json의 sha256. 내려받은 뒤 이 값과 다르면 빌드를 멈춘다.
SHA256 = "c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539"
URL = f"https://huggingface.co/{REPO}/resolve/{REVISION}/tokenizer.json"
