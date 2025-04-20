import logging
import socket
import struct
import time
from typing import Dict, Any, Tuple, Optional, TYPE_CHECKING # TYPE_CHECKING ইম্পোর্ট করা হয়েছে

import smpplib.command
import smpplib.consts
import smpplib.exceptions
import smpplib.pdu

from smpp_server.handlers import SMPPMessageHandler # Handler ইম্পোর্ট করা হয়েছে

logger = logging.getLogger(__name__)

# Circular import এড়ানোর জন্য: শুধুমাত্র টাইপ চেকিং এর সময় SMPPServer ইম্পোর্ট করুন
if TYPE_CHECKING:
    from smpp_server.server import SMPPServer

class SMPPConnection:
    """SMPP Connection handler for a single client."""

    def __init__(
        self,
        socket: socket.socket,
        address: Tuple[str, int],
        config: Dict[str, Any],
        message_handler: SMPPMessageHandler,
        server: 'SMPPServer', # <-- এখানে 'SMPPServer' (কোটেশন সহ স্ট্রিং literal) ব্যবহার করা হয়েছে
    ):
        self.socket = socket
        self.address = address
        self.config = config
        self.message_handler = message_handler
        self.server = server

        self.system_id = config["server"]["system_id"]
        self.password = config["server"]["password"]
        self.version = config["server"]["version"]
        self.timeout = config["server"]["timeout"]

        self.bound = False
        self.bind_type: Optional[int] = None
        self.client_system_id: Optional[str] = None
        self.sequence_number = 0
        self.last_activity = time.time()

        self.socket.settimeout(self.timeout)

    def handle(self):
        logger.info(f"Handler started for connection from {self.address}")
        try:
            while self.server.running:
                if time.time() - self.last_activity > self.timeout:
                    logger.warning(f"Connection from {self.address} timed out (no activity for {self.timeout}s)")
                    break

                try:
                    header_len = 16
                    header = self.socket.recv(header_len)

                    if not header:
                        logger.info(f"Connection from {self.address} closed by client")
                        break

                    if len(header) < header_len:
                        logger.error(f"Received incomplete header from {self.address}")
                        break

                    command_length, command_id, command_status, sequence_number = struct.unpack('>IIII', header)

                    if command_length < header_len:
                        logger.error(f"Invalid PDU length ({command_length}) received from {self.address}")
                        break

                    body_len = command_length - header_len
                    body = b''
                    while len(body) < body_len:
                        remaining_len = body_len - len(body)
                        chunk = self.socket.recv(remaining_len)
                        if not chunk:
                            logger.error(f"Connection closed while reading PDU body from {self.address}")
                            return
                        body += chunk

                    pdu_bytes = header + body

                    try:
                        pdu = smpplib.pdu.PDU.from_bytes(pdu_bytes)
                        self.last_activity = time.time()
                        logger.debug(f"Received PDU: {pdu.command_id_name} (seq={pdu.sequence_number}) from {self.address}")

                        self.handle_pdu(pdu)

                        self.server.track_transaction()

                    except smpplib.exceptions.PDUParseError as e:
                         logger.error(f"Failed to parse PDU from {self.address}: {e}")
                         self.send_generic_nack(smpplib.consts.SMPP_ESME_R_BADCMDID, sequence_number)
                         continue

                    except Exception as e:
                        logger.error(f"Error handling PDU from {self.address}: {e}", exc_info=True)
                        self.send_generic_nack(smpplib.consts.SMPP_ESME_R_SYSFAIL, sequence_number)
                        continue


                except socket.timeout:
                    logger.debug(f"Socket read timeout on connection from {self.address}")
                    continue


                except Exception as e:
                    logger.error(f"Socket error on connection from {self.address}: {e}", exc_info=True)
                    break

        finally:
            self.close()
            self.server.remove_connection(self)
            logger.info(f"Handler stopped for connection from {self.address}")


    def handle_pdu(self, pdu: smpplib.pdu.PDU):
        logger.info(f"Processing PDU: {pdu.command_id_name} (seq={pdu.sequence_number}) from {self.address}")

        if not self.bound and pdu.command_id not in [smpplib.consts.SMPP_CMD_BIND_RECEIVER,
                                                     smpplib.consts.SMPP_CMD_BIND_TRANSMITTER,
                                                     smpplib.consts.SMPP_CMD_BIND_TRANSCEIVER,
                                                     smpplib.consts.SMPP_CMD_ENQUIRE_LINK,
                                                     smpplib.consts.SMPP_CMD_GENERIC_NACK]:
            logger.warning(f"Received {pdu.command_id_name} (seq={pdu.sequence_number}) before bind from {self.address}")
            self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_NOTBOUND))
            return

        if pdu.command_id in [smpplib.consts.SMPP_CMD_BIND_RECEIVER,
                              smpplib.consts.SMPP_CMD_BIND_TRANSMITTER,
                              smpplib.consts.SMPP_CMD_BIND_TRANSCEIVER]:
            try:
                 if pdu.system_id == self.system_id and pdu.password == self.password:
                      auth_success = True
                      self.client_system_id = pdu.system_id
                 else:
                      auth_success = False

                 if auth_success:
                     self.bound = True
                     self.bind_type = pdu.command_id
                     logger.info(f"Connection from {self.address} bound successfully as {pdu.command_id_name} with system_id '{self.client_system_id}'")
                     response_pdu = pdu.create_response(smpplib.consts.SMPP_ESME_R_OK)
                     response_pdu.system_id = self.system_id.encode('ascii')
                     self.send_response(response_pdu)
                 else:
                     logger.warning(f"Authentication failed for bind request from {self.address} (system_id: {pdu.system_id})")
                     self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_BINDFAIL))
                     time.sleep(1)
                     self.close()

            except Exception as e:
                 logger.error(f"Error during bind processing for {self.address}: {e}", exc_info=True)
                 self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_SYSFAIL))
                 time.sleep(1)
                 self.close()


        elif pdu.command_id == smpplib.consts.SMPP_CMD_SUBMIT_SM:
            if self.bind_type not in [smpplib.consts.SMPP_CMD_BIND_TRANSMITTER, smpplib.consts.SMPP_CMD_BIND_TRANSCEIVER]:
                 logger.warning(f"Received SUBMIT_SM from {self.address} which is not bound as TX or TRX")
                 self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_INVCMDID))
                 return

            try:
                 logger.info(f"Received SUBMIT_SM (seq={pdu.sequence_number}) from {self.address}")
                 message_id = self.message_handler.process_submit_sm(pdu, self.client_system_id)

                 response_pdu = pdu.create_response(smpplib.consts.SMPP_ESME_R_OK)
                 response_pdu.message_id = str(message_id) if message_id is not None else ''

                 self.send_response(response_pdu)
                 self.server.messages_counter.labels(type='submit_sm', status='success').inc()

            except Exception as e:
                 logger.error(f"Error processing SUBMIT_SM from {self.address}: {e}", exc_info=True)
                 self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_SUBMITFAIL))
                 self.server.messages_counter.labels(type='submit_sm', status='failed').inc()


        elif pdu.command_id == smpplib.consts.SMPP_CMD_DELIVER_SM:
             if self.bind_type not in [smpplib.consts.SMPP_CMD_BIND_RECEIVER, smpplib.consts.SMPP_CMD_BIND_TRANSCEIVER]:
                  logger.warning(f"Received DELIVER_SM from {self.address} which is not bound as RX or TRX")
                  self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_INVCMDID))
                  return

             try:
                  logger.info(f"Received DELIVER_SM (DLR?) (seq={pdu.sequence_number}) from {self.address}")
                  self.message_handler.process_deliver_sm(pdu)

                  self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_OK))
                  self.server.messages_counter.labels(type='deliver_sm', status='success').inc()

             except Exception as e:
                  logger.error(f"Error processing DELIVER_SM from {self.address}: {e}", exc_info=True)
                  self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_DELIVERYFAILURE))
                  self.server.messages_counter.labels(type='deliver_sm', status='failed').inc()


        elif pdu.command_id == smpplib.consts.SMPP_CMD_ENQUIRE_LINK:
            logger.debug(f"Received ENQUIRE_LINK (seq={pdu.sequence_number}) from {self.address}")
            self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_OK))
            self.last_activity = time.time()


        elif pdu.command_id == smpplib.consts.SMPP_CMD_UNBIND:
            logger.info(f"Received UNBIND (seq={pdu.sequence_number}) from {self.address}")
            self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_OK))
            self.bound = False
            time.sleep(1)
            self.close()


        elif pdu.command_id == smpplib.consts.SMPP_CMD_GENERIC_NACK:
            logger.warning(f"Received GENERIC_NACK (seq={pdu.sequence_number}, status={pdu.command_status}) from {self.address}")

        else:
            logger.warning(f"Received unsupported PDU command ID {pdu.command_id} ({pdu.command_id_name}) from {self.address}")
            self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_INVCMDID))


    def send_pdu(self, pdu: smpplib.pdu.PDU):
        if not self.server.running:
            logger.warning(f"Attempted to send PDU ({pdu.command_id_name}) but server is not running.")
            return
        if self.socket.fileno() == -1:
             logger.warning(f"Attempted to send PDU ({pdu.command_id_name}) but socket is closed for {self.address}.")
             return

        try:
            pdu_bytes = pdu.to_bytes()
            self.socket.sendall(pdu_bytes)
            self.last_activity = time.time()
            logger.debug(f"Sent PDU: {pdu.command_id_name} (seq={pdu.sequence_number}) to {self.address}")
        except Exception as e:
            logger.error(f"Error sending PDU ({pdu.command_id_name}) to {self.address}: {e}", exc_info=True)
            self.close()

    def send_response(self, response_pdu: smpplib.pdu.PDU):
         self.send_pdu(response_pdu)

    def send_generic_nack(self, command_status: int, sequence_number: int):
        generic_nack_pdu = smpplib.command.GenericNack(
            command_status=command_status,
            sequence_number=sequence_number
        )
        self.send_pdu(generic_nack_pdu)
        logger.warning(f"Sent GENERIC_NACK (status={command_status}, seq={sequence_number}) to {self.address}")