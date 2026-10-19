FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /loadgen
COPY loadgen.py .
RUN useradd -r -u 10002 loadgen
USER loadgen
ENTRYPOINT ["python", "loadgen.py"]