# 파이프라인 탐험 사이트 S0. 실행 중에는 인터넷을 쓰지 않는다(토크나이저는 빌드 때 한 번만 받는다).
FROM python:3.12-slim
# .pyc를 만들지 않고, 로그를 버퍼 없이 바로 내보낸다
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

# 1) 의존성 (requirements가 바뀔 때만 이 층을 다시 만든다)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2) 토크나이저: 고정한 revision에서 받아 sha256이 다르면 빌드를 멈춘다 (값은 tokenizer_pin.py 한 곳)
COPY tokenizer_pin.py fetch_tokenizer.py ./
RUN python fetch_tokenizer.py /opt/tokenizer/tokenizer.json \
 && chmod 0444 /opt/tokenizer/tokenizer.json

# 3) 앱 코드
COPY app.py limits.py ollama.py streamparse.py tokenization.py traces.py ./
COPY static ./static

# 비root 사용자(uid 10004). 요청 기록용 /data는 이 사용자가 쓸 수 있게 한다(새 볼륨이 이 소유권을 이어받는다)
RUN useradd -r -u 10004 explorer \
 && mkdir -p /data \
 && chown explorer /data
USER explorer

ENV TOKENIZER_PATH=/opt/tokenizer/tokenizer.json TRACE_PATH=/data/traces.jsonl
EXPOSE 8100
# -w 1: 'Ollama 호출은 한 번에 하나' 자물쇠가 프로세스 안에 있으므로 워커는 1개. --threads: 스트리밍 중에도 토큰화 요청을 받는다.
# --timeout: 스트리밍이 길어져도 워커가 죽지 않게 넉넉히
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:8100", "--timeout", "300", "app:app"]
