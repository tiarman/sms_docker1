FROM python:3.9-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    netcat-openbsd \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY src/smpp_server/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY src/smpp_server /app/smpp_server
COPY src/common /app/common

RUN mkdir -p /etc/smpp-server /var/log/smpp-server

COPY deploy/docker/smpp-server/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 2775 8080

ENV PYTHONPATH=/app

ENTRYPOINT ["/entrypoint.sh"]