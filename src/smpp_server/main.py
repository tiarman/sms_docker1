import logging
import os
import signal
import sys
import threading
import time
from typing import Dict, Any

import smpplib.client
import smpplib.consts
import smpplib.gsm
import uvicorn
from fastapi import FastAPI
from prometheus_client import Counter, Gauge, start_http_server

from smpp_server.api import create_api
from smpp_server.config import load_config
from smpp_server.db import init_db
from smpp_server.handlers import SMPPMessageHandler
from smpp_server.server import SMPPServer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

CONNECTIONS_GAUGE = Gauge('smpp_active_connections', 'Number of active SMPP connections')
MESSAGES_COUNTER = Counter('smpp_messages_total', 'Total number of SMPP messages processed', ['type', 'status'])
TPS_GAUGE = Gauge('smpp_current_tps', 'Current transactions per second')

def main():
    logger.info("Starting SMPP server...")

    config = load_config()

    init_db(config)

    message_handler = SMPPMessageHandler(config)

    # start_http_server(8000) # Prometheus metric server

    server = SMPPServer(
        config=config,
        message_handler=message_handler,
        connections_gauge=CONNECTIONS_GAUGE,
        messages_counter=MESSAGES_COUNTER,
        tps_gauge=TPS_GAUGE,
    )

    server_thread = threading.Thread(target=server.start)
    server_thread.daemon = True
    server_thread.start()

    api = create_api(server, config)

    def signal_handler(sig, frame):
        logger.info("Shutting down SMPP server...")
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("Starting API server...")
    uvicorn.run(
        api,
        host="0.0.0.0",
        port=8080,
        log_level="info"
    )

if __name__ == "__main__":
    main()