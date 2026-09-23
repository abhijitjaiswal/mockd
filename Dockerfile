# mockd — the console and the mock it starts, in one image.
#
# Two ports matter: 4100 is the console you open in a browser, 4010 is the mock
# it starts for your UI to point at. Both bind 0.0.0.0 in here (set by
# docker-compose) because a container's loopback is not yours.

FROM python:3.12-slim

# curl is for the healthcheck; nothing else is needed at runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# dependencies first, so editing the code does not re-install them
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a normal user. The writable directories are created and handed over
# before dropping privileges, so a mounted volume still works.
RUN useradd --create-home --uid 10001 mockd \
 && mkdir -p /app/logs /app/specs /app/tests/drafts \
 && chown -R mockd:mockd /app
USER mockd

ENV PYTHONUNBUFFERED=1 \
    CONSOLE_HOST=0.0.0.0 \
    MOCK_HOST=0.0.0.0 \
    CONSOLE_PORT=4100

EXPOSE 4100 4010

HEALTHCHECK --interval=15s --timeout=4s --start-period=10s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${CONSOLE_PORT}/api/defaults" > /dev/null || exit 1

CMD ["python", "console.py"]
