FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /watcher
COPY *.py .
RUN useradd -r -u 10003 watcher
USER watcher
CMD ["python", "main.py"]