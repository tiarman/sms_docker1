import logging
import socket
import threading
import time
from typing import Dict, Any, List, Optional

import smpplib.command
import smpplib.consts
import smpplib.exceptions
import smpplib.pdu
from prometheus_client import Counter, Gauge

from smpp_server.connections import SMPPConnection
from smpp_server.handlers import SMPPMessageHandler

logger = logging.getLogger(__name__)

# from smpp_server.server import SMPPServer

class SMPPServer:
    """SMPP Server implementation."""

    def __init__(
        self,
        config: Dict[str, Any],
        message_handler: SMPPMessageHandler,
        connections_gauge: Gauge,
        messages_counter: Counter,
        tps_gauge: Gauge,
    ):
        self.config = config
        self.message_handler = message_handler
        self.connections_gauge = connections_gauge
        self.messages_counter = messages_counter
        self.tps_gauge = tps_gauge

        self.host = config["server"]["host"]
        self.port = config["server"]["port"]
        self.system_id = config["server"]["system_id"]
        self.password = config["server"]["password"]
        self.system_type = config["server"]["system_type"]
        self.version = config["server"]["version"]
        self.max_connections = config["server"]["max_connections"]
        self.timeout = config["server"]["timeout"]
        self.tps_limit = config["server"]["tps_limit"]

        self.running = False
        self.server_socket: Optional[socket.socket] = None
        self.connections: List[SMPPConnection] = []
        self.connection_lock = threading.Lock()

        self.transaction_count = 0
        self.last_second = int(time.time())
        self.tps_lock = threading.Lock()

    def start(self):
        logger.info(f"Starting SMPP server on {self.host}:{self.port}")

        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(5)

            self.running = True
            logger.info("SMPP server started successfully.")

            tps_thread = threading.Thread(target=self._tps_tracker)
            tps_thread.daemon = True
            tps_thread.start()
            logger.info("TPS tracker started.")

            while self.running:
                try:
                    client_socket, address = self.server_socket.accept()
                    logger.info(f"New connection from {address}")

                    with self.connection_lock:
                        if len(self.connections) >= self.max_connections:
                            logger.warning(f"Maximum connections ({self.max_connections}) reached, rejecting connection from {address}")
                            client_socket.close()
                            continue

                    connection = SMPPConnection(
                        socket=client_socket,
                        address=address,
                        config=self.config,
                        message_handler=self.message_handler,
                        server=self,
                    )

                    connection_thread = threading.Thread(target=connection.handle)
                    connection_thread.daemon = True
                    connection_thread.start()

                    with self.connection_lock:
                        self.connections.append(connection)
                        self.connections_gauge.set(len(self.connections))

                except socket.timeout:
                     pass

                except Exception as e:
                    if self.running:
                        logger.error(f"Error accepting connection: {e}", exc_info=True)
                    else:
                        logger.info("Server is shutting down, stopping accept loop.")

        except Exception as e:
            logger.error(f"Failed to start SMPP server: {e}", exc_info=True)

        finally:
            self.stop()
            logger.info("SMPP server stopped.")


    def stop(self):
        if not self.running:
            logger.info("SMPP server is already stopped.")
            return

        logger.info("Stopping SMPP server...")
        self.running = False

        with self.connection_lock:
            for connection in reversed(self.connections):
                 logger.info(f"Closing connection from {connection.address}")
                 connection.close()

            self.connections.clear()
            self.connections_gauge.set(0)

        if self.server_socket:
            try:
                self.server_socket.shutdown(socket.SHUT_RDWR)
                self.server_socket.close()
                logger.info("Server socket closed.")
            except Exception as e:
                logger.error(f"Error closing server socket: {e}")

        logger.info("SMPP server shutdown complete.")


    def remove_connection(self, connection: 'SMPPConnection'):
        with self.connection_lock:
            if connection in self.connections:
                self.connections.remove(connection)
                self.connections_gauge.set(len(self.connections))
                logger.info(f"Removed connection from {connection.address}. Active connections: {len(self.connections)}")


    def track_transaction(self):
        with self.tps_lock:
            self.transaction_count += 1


    def _tps_tracker(self):
        logger.info("TPS tracker thread started.")
        while self.running:
            current_second = int(time.time())
            if current_second > self.last_second:
                with self.tps_lock:
                    current_tps = self.transaction_count / (current_second - self.last_second) if (current_second - self.last_second) > 0 else 0
                    self.tps_gauge.set(current_tps)
                    self.transaction_count = 0
                    self.last_second = current_second

            time.sleep(0.1)
        logger.info("TPS tracker thread stopped.")