import logging
import socket
import struct
import time
from typing import Dict, Any, Tuple, Optional, TYPE_CHECKING # TYPE_CHECKING ইম্পোর্ট করা হয়েছে

import smpplib.command
import smpplib.consts
import smpplib.exceptions
import smpplib.pdu

from smpp_server.handlers import SMPPMessageHandler # Handler ইম্পোর্ট করা হয়েছে
from smpp_server.db import get_db, get_user_by_system_id # <-- get_db এবং get_user_by_system_id ইম্পোর্ট করা হয়েছে

logger = logging.getLogger(__name__)

# Circular import এড়ানোর জন্য: শুধুমাত্র টাইপ চেকিং এর সময় SMPPServer ইম্পোর্ট করুন
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
        server: 'SMPPServer', # <-- এখানে 'SMPPServer' (কোটেশন সহ স্ট্রিং literal) ব্যবহার করা হয়েছে
    ):
        self.socket = socket
        self.address = address
        self.config = config
        self.message_handler = message_handler
        self.server = server

        # এই সার্ভারের গ্লোবাল ক্রেডেনশিয়াল, আসল চেক ডাটাবেস থেকে হবে
        self.system_id = config["server"]["system_id"]
        self.password = config["server"]["password"] # এই ভ্যারিয়েবলটি এখন Bind অথেন্টিকেশনে ব্যবহার হবে না

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

        # বাইন্ড হওয়া ছাড়া কিছু কমান্ড অনুমতি আছে
        if not self.bound and pdu.command_id not in [smpplib.consts.SMPP_CMD_BIND_RECEIVER,
                                                     smpplib.consts.SMPP_CMD_BIND_TRANSMITTER,
                                                     smpplib.consts.SMPP_CMD_BIND_TRANSCEIVER,
                                                     smpplib.consts.SMPP_CMD_ENQUIRE_LINK,
                                                     smpplib.consts.SMPP_CMD_GENERIC_NACK]:
            logger.warning(f"Received {pdu.command_id_name} (seq={pdu.sequence_number}) before bind from {self.address}")
            self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_NOTBOUND))
            return

        # --- Handle different PDU types ---

        # Bind PDUs - ডাটাবেস দিয়ে অথেন্টিকেশন
        if pdu.command_id in [smpplib.consts.SMPP_CMD_BIND_RECEIVER,
                              smpplib.consts.SMPP_CMD_BIND_TRANSMITTER,
                              smpplib.consts.SMPP_CMD_BIND_TRANSCEIVER]:
            auth_success = False
            user = None
            db_session = None
            try:
                 # get_db() generator থেকে ডাটাবেস সেশন নিন
                 # এটি একটি স্ট্যান্ডার্ড পদ্ধতি, ফাংশনটি সেশন প্রোভাইড করে এবং finally তে ক্লোজ করে
                 db_session_generator = get_db()
                 db_session = next(db_session_generator)
                 logger.debug("DB Session obtained for bind handling.")

                 # system_id দিয়ে ইউজারকে খুঁজুন
                 # pdu.system_id হলো bytes, এটিকে decode করতে হবে
                 requested_system_id = pdu.system_id.decode('ascii')
                 requested_password = pdu.password.decode('ascii') # পাসওয়ার্ডও decode করুন

                 user = get_user_by_system_id(db_session, requested_system_id)

                 if user and user.is_active:
                     # ইউজার পাওয়া গেলে এবং সক্রিয় থাকলে পাসওয়ার্ড চেক করুন
                     # TODO: আসল অ্যাপে পাসওয়ার্ড হ্যাশিং চেক করুন (যেমন bcrypt বা argon2)
                     if user.password == requested_password: # টেম্পোরারি প্লেইন টেক্সট পাসওয়ার্ড চেক
                          auth_success = True
                          self.client_system_id = user.system_id # ক্লায়েন্টের আসল system_id সেভ করুন
                          logger.info(f"Authentication successful for system_id '{self.client_system_id}' from {self.address}")
                          # TODO: ইউজারের Bind Type (TX/RX/TRX) অথরাইজেশন চেক করুন

                     else:
                          logger.warning(f"Authentication failed for system_id '{requested_system_id}' from {self.address}: Invalid password")
                          # TODO: ফেইলড লগইন প্রচেষ্টা ট্র্যাক করুন (সিকিউরিটি)
                 elif user and not user.is_active:
                      logger.warning(f"Authentication failed for system_id '{requested_system_id}' from {self.address}: User inactive")
                 else:
                      logger.warning(f"Authentication failed: system_id '{requested_system_id}' not found from {self.address}")

            except Exception as e:
                 # ডাটাবেস অ্যাক্সেস বা অন্য কোনো এরর
                 logger.error(f"Error during bind authentication for {self.address}: {e}", exc_info=True)
                 auth_success = False # নিশ্চিত করা যে অথেন্টিকেশন সফল হয়নি

            finally:
                # Ensure the database session is closed
                if db_session_generator:
                     try:
                         # Generator কে ক্লোজ করে finally ব্লক এক্সিকিউট করা হয়
                         db_session_generator.close()
                         logger.debug("DB Session generator closed.")
                     except StopIteration:
                          pass # Generator 이미 শেষ হয়ে গেলে StopIteration হয়
                # অথবা যদি শুধু next(get_db()) ব্যবহার করেন, তাহলে এখানে if db_session: db_session.close() যথেষ্ট।


            if auth_success: # যদি অথেন্টিকেশন সফল হয় (এবং অথরাইজেশন)
                 self.bound = True
                 self.bind_type = pdu.command_id # ক্লায়েন্ট যে টাইপে বাইন্ড করতে চেয়েছে

                 logger.info(f"Connection from {self.address} bound successfully as {pdu.command_id_name} with system_id '{self.client_system_id}'")
                 response_pdu = pdu.create_response(smpplib.consts.SMPP_ESME_R_OK)
                 response_pdu.system_id = self.system_id.encode('ascii') # সার্ভারের system_id বাইন্ড রেসপন্সে পাঠান
                 self.send_response(response_pdu)
                 # TODO: Prometheus মেট্রিক আপডেট করুন (সফল বাইন্ড)

            else: # যদি অথেন্টিকেশন বা অথরাইজেশন সফল না হয়
                 logger.warning(f"Bind failed for {self.address}")
                 self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_BINDFAIL)) # বাইন্ড ফেইল রেসপন্স
                 # অথেন্টিকেশন ফেইল হলে কানেকশন বন্ধ করা উচিত
                 time.sleep(1) # রেসপন্স পাঠানোর সুযোগ দেওয়া
                 self.close() # কানেকশন ক্লোজ করা


        # Submit SM (Sending Message)
        elif pdu.command_id == smpplib.consts.SMPP_CMD_SUBMIT_SM:
            # Ensure bound as Transmitter (TX) or Transceiver (TRX)
            if self.bind_type not in [smpplib.consts.SMPP_CMD_BIND_TRANSMITTER, smpplib.consts.SMPP_CMD_BIND_TRANSCEIVER]:
                 logger.warning(f"Received SUBMIT_SM from {self.address} which is not bound as TX or TRX")
                 self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_INVCMDID)) # অবৈধ কমান্ড আইডি রেসপন্স
                 return

            # Message Handling Logic
            try:
                 logger.info(f"Received SUBMIT_SM (seq={pdu.sequence_number}) from {self.address}")
                 # message_handler কে PDU পাস করা
                 # TODO: আসল অ্যাপে DB সেশন message_handler এ পাস করতে হবে
                 message_id = self.message_handler.process_submit_sm(pdu, self.client_system_id) # এই মেথড আপনাকে handlers.py তে সম্পূর্ণ করতে হবে

                 # Submit SM Response (Enquire Link)
                 response_pdu = pdu.create_response(smpplib.consts.SMPP_ESME_R_OK)
                 # message_id রেসপন্সে যোগ করা (optional কিন্তু স্ট্যান্ডার্ড প্র্যাকটিস)
                 response_pdu.message_id = str(message_id) if message_id is not None else '' # message_id স্ট্রিং হিসাবে পাঠান

                 self.send_response(response_pdu)
                 self.server.messages_counter.labels(type='submit_sm', status='success').inc() # মেট্রিক আপডেট

            except Exception as e:
                 logger.error(f"Error processing SUBMIT_SM from {self.address}: {e}", exc_info=True)
                 self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_SUBMITFAIL)) # সাবমিট ফেইল রেসপンス
                 self.server.messages_counter.labels(type='submit_sm', status='failed').inc()


        # Deliver SM (Receiving Message/DLR)
        elif pdu.command_id == smpplib.consts.SMPP_CMD_DELIVER_SM:
             # Ensure bound as Receiver (RX) or Transceiver (TRX)
             if self.bind_type not in [smpplib.consts.SMPP_CMD_BIND_RECEIVER, smpplib.consts.SMPP_CMD_BIND_TRANSCEIVER]:
                  logger.warning(f"Received DELIVER_SM from {self.address} which is not bound as RX or TRX")
                  self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_INVCMDID))
                  return

             # DLR Handling Logic
             # আপনার message_handler অবজেক্ট ব্যবহার করে DLR প্রসেস করা হবে
             try:
                  logger.info(f"Received DELIVER_SM (DLR?) (seq={pdu.sequence_number}) from {self.address}")
                  # TODO: আসল অ্যাপে DB সেশন message_handler এ পাস করতে হবে
                  self.message_handler.process_deliver_sm(pdu) # এই মেথড আপনাকে handlers.py তে সম্পূর্ণ করতে হবে

                  # Deliver SM Response
                  self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_OK))
                  self.server.messages_counter.labels(type='deliver_sm', status='success').inc()

             except Exception as e:
                  logger.error(f"Error processing DELIVER_SM from {self.address}: {e}", exc_info=True)
                  self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_DELIVERYFAILURE)) # ডেলিভারি ফেইল রেসপন্স
                  self.server.messages_counter.labels(type='deliver_sm', status='failed').inc()


        # Enquire Link (Keep-alive)
        elif pdu.command_id == smpplib.consts.SMPP_CMD_ENQUIRE_LINK:
            logger.debug(f"Received ENQUIRE_LINK (seq={pdu.sequence_number}) from {self.address}")
            self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_OK))
            self.last_activity = time.time()


        # Unbind
        elif pdu.command_id == smpplib.consts.SMPP_CMD_UNBIND:
            logger.info(f"Received UNBIND (seq={pdu.sequence_number}) from {self.address}")
            self.send_response(pdu.create_response(smpplib.consts.SMPP_ESME_R_OK))
            self.bound = False
            time.sleep(1)
            self.close()


        # Generic NACK (Negative Acknowledgment) - Received for malformed PDUs
        elif pdu.command_id == smpplib.consts.SMPP_CMD_GENERIC_NACK:
            logger.warning(f"Received GENERIC_NACK (seq={pdu.sequence_number}, status={pdu.command_status}) from {self.address}")

        # Other PDU types (Data_SM, Cancel_SM, Replace_SM etc.)
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