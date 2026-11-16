FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN useradd --create-home --uid 10001 monitor \
    && mkdir -p /data /config \
    && chown -R monitor:monitor /data /config

COPY pyproject.toml README.md requirements.txt LICENSE ./
COPY status_monitor ./status_monitor
COPY config.example.yaml /config/config.yaml

RUN pip install --no-cache-dir .

USER monitor
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

ENTRYPOINT ["status-monitor"]
CMD ["--config", "/config/config.yaml", "--log-dir", "/data", "--web", "--web-host", "0.0.0.0", "--web-port", "8080"]
