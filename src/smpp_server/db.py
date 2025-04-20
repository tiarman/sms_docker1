import logging
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, BigInteger, Float, Boolean, ForeignKey, Enum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from sqlalchemy.exc import OperationalError, ProgrammingError
import enum
import time
from typing import Dict, Any, Generator, Optional, List # Optional এবং List যোগ করা হয়েছে

# Local imports
from smpp_server.config import load_config # কনফিগারেশন লোড করার জন্য

logger = logging.getLogger(__name__)

# SQLAlchemy Setup
# Base ক্লাস তৈরি করুন যা থেকে আপনার সব মডেল ইনহেরিট করবে
Base = declarative_base()

# ডাটাবেস ইঞ্জিন এবং সেশনমেকার ইনিশিয়ালাইজ করার জন্য গ্লোবাল ভ্যারিয়েবল
# এইগুলো পরে init_db ফাংশনে সেট করা হবে
engine = None
SessionLocal = None

# ============================================================================
# Database Models
# আপনার প্রজেক্টের প্রয়োজন অনুযায়ী এখানে আপনার সব টেবিলের মডেল সংজ্ঞায়িত করুন।
# এই মডেলগুলো আপনার গাইডের ফিচার অনুযায়ী প্রয়োজনীয় কিছু উদাহরণ।
# ============================================================================

# User Model (for SMPP client or API user)
class User(Base):
    __tablename__ = 'users' # ডাটাবেসের টেবিলের নাম

    id = Column(Integer, primary_key=True, index=True) # প্রাইমারি কী
    system_id = Column(String, unique=True, index=True, nullable=False) # SMPP system_id
    password = Column(String, nullable=False) # পাসওয়ার্ড (সিকিউরিটি হ্যাশিং ব্যবহার করা উচিত)
    system_type = Column(String, nullable=True)
    address_range = Column(String, nullable=True) # প্রেরকের ঠিকানা পরিসীমা
    max_tps = Column(Integer, default=100) # প্রতি সেকেন্ডে সর্বোচ্চ লেনদেন
    is_active = Column(Boolean, default=True) # ইউজার সক্রিয় কিনা
    created_at = Column(DateTime, default=lambda: time.time()) # তৈরি হওয়ার সময়
    updated_at = Column(DateTime, default=lambda: time.time(), onupdate=lambda: time.time()) # আপডেট হওয়ার সময়

    # রিলেশনশিপ (Optional)
    # messages = relationship("Message", back_populates="user")


# Message Model (for storing sent/received messages and DLRs)
class Message(Base):
    __tablename__ = 'messages' # ডাটাবেসের টেবিলের নাম

    id = Column(BigInteger, primary_key=True, index=True) # প্রাইমারি কী (মেসেজের পরিমাণ বেশি হতে পারে তাই BigInteger)
    message_id = Column(String, unique=True, index=True, nullable=True) # SMPP বা Vendor মেসেজ ID (Vendor থেকে এলে)
    client_message_id = Column(String, index=True, nullable=True) # ক্লায়েন্টের নিজস্ব মেসেজ ID (যদি ক্লায়েন্ট পাঠায়)

    # মেসেজ স্ট্যাটাস
    class MessageStatus(enum.Enum):
        PENDING = "PENDING"
        QUEUED = "QUEUED"
        SENT = "SENT" # Vendor কে পাঠানো হয়েছে
        DELIVERED = "DELIVERED" # DLR received for success
        FAILED = "FAILED" # DLR received for failure
        EXPIRED = "EXPIRED" # Vendor বা System timeout
        REJECTED = "REJECTED" # সিস্টেম কর্তৃক ভ্যালিডেশন বা অন্য কারণে রিজেক্টেড
        UNKNOWN = "UNKNOWN" # স্ট্যাটাস জানা নেই

    status = Column(Enum(MessageStatus), default=MessageStatus.PENDING, nullable=False)

    source_addr = Column(String, nullable=False) # প্রেরকের ঠিকানা
    destination_addr = Column(String, nullable=False) # প্রাপকের ঠিকানা
    short_message = Column(Text, nullable=False) # মেসেজ কনটেন্ট
    encoding = Column(String, nullable=True) # মেসেজ এনকোডিং (যেমন GSM, UCS2)

    # সময় সম্পর্কিত ফিল্ড
    created_at = Column(DateTime, default=lambda: time.time()) # তৈরি হওয়ার সময় (সিস্টেমে ঢোকার সময়)
    queued_at = Column(DateTime, nullable=True) # কিউতে যোগ হওয়ার সময়
    sent_at = Column(DateTime, nullable=True) # ভেন্ডরকে পাঠানোর সময়
    delivered_at = Column(DateTime, nullable=True) # ডেলিভার হওয়ার সময়

    # খরচ ও মূল্য সম্পর্কিত ফিল্ড
    cost = Column(Float, default=0.0, nullable=False) # মেসেজ পাঠানোর খরচ (ভেন্ডরকে দিতে হবে)
    price = Column(Float, default=0.0, nullable=False) # কাস্টমারের কাছ থেকে নেওয়া মূল্য
    currency = Column(String, default="USD", nullable=False) # মুদ্রা

    # রাউটিং এবং ভেন্ডর সম্পর্কিত ফিল্ড
    routed_vendor_id = Column(Integer, ForeignKey('vendors.id'), nullable=True) # কোন ভেন্ডরের মাধ্যমে পাঠানো হয়েছে
    vendor_error_code = Column(String, nullable=True) # ভেন্ডর থেকে আসা এরর কোড
    system_error_code = Column(String, nullable=True) # সিস্টেম জেনারেটেড এরর কোড

    # ক্লায়েন্ট বা ইউজার সম্পর্কিত ফিল্ড
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False) # কোন ইউজার বা ক্লায়েন্ট মেসেজটি পাঠিয়েছে

    # রিলেশনশিপ (Optional)
    # user = relationship("User", back_populates="messages")
    # vendor = relationship("Vendor", back_populates="messages")


# Vendor Model
class Vendor(Base):
    __tablename__ = 'vendors' # ডাটাবেসের টেবিলের নাম

    id = Column(Integer, primary_key=True, index=True) # প্রাইমারি কী
    name = Column(String, unique=True, nullable=False) # ভেন্ডরের নাম (ইউনিক)
    protocol = Column(String, default="SMPP", nullable=False) # প্রোটোকল (SMPP, HTTP)
    smpp_host = Column(String, nullable=True) # SMPP হোস্ট বা IP
    smpp_port = Column(Integer, nullable=True) # SMPP পোর্ট
    smpp_system_id = Column(String, nullable=True) # SMPP system_id
    smpp_password = Column(String, nullable=True) # SMPP পাসওয়ার্ড (সিকিউরিটি হ্যাশিং ব্যবহার করা উচিত)
    http_api_url = Column(String, nullable=True) # যদি HTTP API ভেন্ডর হয়
    http_api_key = Column(String, nullable=True) # HTTP API Key (যদি থাকে)
    is_active = Column(Boolean, default=True) # ভেন্ডর সক্রিয় কিনা
    status = Column(String, default="Unknown") # ভেন্ডরের বর্তমান কানেকশন স্ট্যাটাস (যেমন Connected, Disconnected)
    current_tps = Column(Integer, default=0) # বর্তমান TPS (মনিটরিং এর জন্য)
    max_tps = Column(Integer, default=500) # ভেন্ডরের সর্বোচ্চ TPS লিমিট
    created_at = Column(DateTime, default=lambda: time.time()) # তৈরি হওয়ার সময়
    updated_at = Column(DateTime, default=lambda: time.time(), onupdate=lambda: time.time()) # আপডেট হওয়ার সময়

    # রিলেশনশিপ (Optional)
    # rates = relationship("VendorRate", back_populates="vendor")
    # messages = relationship("Message", back_populates="vendor")


# Vendor Rate Model
class VendorRate(Base):
    __tablename__ = 'vendor_rates' # ডাটাবেসের টেবিলের নাম

    id = Column(Integer, primary_key=True, index=True) # প্রাইমারি কী
    vendor_id = Column(Integer, ForeignKey('vendors.id'), nullable=False) # কোন ভেন্ডরের রেট (Foreign Key)
    country_code = Column(String, index=True, nullable=False) # যেমন BD, IN, 1 (ISO 3166-1 alpha-2 বা E.164)
    operator_code = Column(String, index=True, nullable=True) # যেমন Grameenphone, Airtel (Optional)
    prefix = Column(String, index=True, nullable=False) # যেমন 88017, 88018, 1415 (E.164 ফরম্যাটের অংশ)
    price = Column(Float, nullable=False) # এই রুটের জন্য ভেন্ডরকে কত দিতে হবে
    effective_date = Column(DateTime, nullable=False) # কবে থেকে কার্যকর
    expiry_date = Column(DateTime, nullable=True) # কবে পর্যন্ত কার্যকর (যদি থাকে)
    created_at = Column(DateTime, default=lambda: time.time()) # তৈরি হওয়ার সময়

    # রিলেশনশিপ (Optional)
    # vendor = relationship("Vendor", back_populates="rates")

# আপনার অন্যান্য মডেল (Customer, CustomerRate, Bill, Payment ইত্যাদি) এখানে যোগ করুন।


# ============================================================================
# Database Initialization and Migration
# ============================================================================

def init_db(config: Dict[str, Any]):
    """
    Initializes the database engine and sessionmaker.
    This function is called once when the application starts.
    """
    global engine, SessionLocal # গ্লোবাল ভ্যারিয়েবলগুলো ব্যবহার করার জন্য

    db_config = config["database"]
    # ডাটাবেস URL তৈরি করুন
    # ensure_ascii=False যোগ করা হয়েছে যদি স্ট্রিং এ ASCII নয় এমন ক্যারেক্টার থাকে
    DATABASE_URL = f"postgresql://{db_config['user']}:{db_config['password']}@{db_config['host']}:{db_config['port']}/{db_config['name']}"

    logger.info(f"Attempting to connect to database at {db_config['host']}:{db_config['port']}/{db_config['name']}")

    try:
        # ডাটাবেস ইঞ্জিন তৈরি করুন
        # pool_pre_ping=True কানেকশন পুলের কানেকশনগুলো ব্যবহারের আগে চেক করে
        # echo=True যোগ করলে SQL স্টেটমেন্ট লগে দেখা যায় (ডেবাগিং এর জন্য ভালো)
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)

        # সেশনমেকার তৈরি করুন
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        # ডাটাবেস কানেকশন টেস্ট করুন
        with engine.connect() as connection:
            connection.execute("SELECT 1") # একটি সাধারণ কোয়েরি চালিয়ে কানেকশন চেক

        logger.info("Database connection successful. Engine and SessionLocal created.")

    except (OperationalError, ProgrammingError) as e:
        logger.error(f"Failed to connect to database: {e}")
        # এখানে এরর হ্যান্ডেলিং লজিক যোগ করুন, যেমন retries বা অ্যাপ্লিকেশন বন্ধ করে দেওয়া
        # আপাতত শুধু এরর লগ করা হচ্ছে, অ্যাপ্লিকেশন হয়তো ক্র্যাশ করবে যদি DB কানেকশন না হয়

    except Exception as e:
        logger.error(f"An unexpected error occurred during database initialization: {e}")
        # অন্যান্য সম্ভাব্য এরর হ্যান্ডেলিং

def migrate(config: Dict[str, Any] = None):
    """
    Runs database migrations.
    Called by the entrypoint script.
    In a real project, this function would use Alembic.
    For simplicity here, it just creates tables based on models if they don't exist.
    """
    logger.info("Database migration function called.")

    # যদি init_db কল না হয়ে থাকে, তাহলে config ব্যবহার করে ইনিশিয়ালাইজ করুন
    if engine is None or SessionLocal is None:
         if config is None:
             config = load_config() # কনফিগারেশন লোড করুন যদি দেওয়া না থাকে
         init_db(config)

         if engine is None: # init_db ফেইল করলে ইঞ্জিন None থাকবে
             logger.error("Database engine not initialized for migrations.")
             return # মাইগ্রেশন চালানো সম্ভব নয়

    # আসল মাইগ্রেশন লজিক (Alembic) এখানে যুক্ত করতে হবে।
    # Alembic সেটআপ করলে এখানে Alembic কমান্ড বা API কল হবে।
    # Alembic সেটআপ ছাড়া মডেল অনুযায়ী টেবিল তৈরি করতে নিচের লাইনটি ব্যবহার করতে পারেন:

    logger.info("Attempting to create database tables (if they don't exist)...")
    try:
        # Base.metadata.create_all() কল করলে আপনার সংজ্ঞায়িত মডেল অনুযায়ী টেবিল তৈরি হবে
        # যদি টেবিলগুলো 이미 থাকে, তাহলে এটি কিছু করবে না।
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables checked/created successfully.")
    except Exception as e:
        logger.error(f"Failed to create database tables: {e}")
        # এরর হ্যান্ডেলিং

    # Alembic ব্যবহার করলে এখানে আপগ্রেড করার কমান্ড বা API কল হবে।


# ============================================================================
# Dependency for getting DB Session (Recommended for API/Handlers)
# ============================================================================

# এই ফাংশনটি FastAPI dependency injection এর সাথে ব্যবহার করা যেতে পারে
# বা হ্যান্ডলারদের জন্য ডাটাবেস সেশন পাওয়ার জন্য কল করা যেতে পারে।
def get_db() -> Generator[Session, None, None]:
    """
    Provides a database session for use with 'with' statement or dependency injection.
    Ensures the session is closed afterwards.
    """
    if SessionLocal is None:
        logger.error("Database SessionLocal is not initialized.")
        # আপনি চাইলে এখানে init_db() কল করতে পারেন, কিন্তু সাধারণত স্টার্টআপেই ইনিশিয়ালাইজ হওয়া উচিত।
        raise Exception("Database not initialized. Call init_db() during startup.") # অথবা অন্য কোনো এরর

    db = SessionLocal()
    try:
        yield db # সেশন প্রোভাইড করা হয়
    finally:
        db.close() # অনুরোধ বা কাজ শেষ হলে সেশন বন্ধ করে দেওয়া হয়


# ============================================================================
# Basic CRUD Operations
# আপনার প্রয়োজন অনুযায়ী এখানে ডাটাবেসের সাথে যোগাযোগের জন্য ফাংশন লিখুন।
# এইগুলো উদাহরণ হিসেবে দেওয়া হলো।
# ============================================================================

# Example: Create a new user
def create_user(db: Session, system_id: str, password: str, system_type: str = None) -> User:
    # পাসওয়ার্ড হ্যাশ করা উচিত আসল অ্যাপ্লিকেশনে
    db_user = User(system_id=system_id, password=password, system_type=system_type)
    db.add(db_user)
    db.commit()
    db.refresh(db_user) # ডেটাবেস থেকে রিফ্রেশ করে আইডি পেতে
    logger.info(f"Created new user with system_id: {db_user.system_id}")
    return db_user

# Example: Get a user by system_id
def get_user_by_system_id(db: Session, system_id: str) -> Optional[User]:
    logger.debug(f"Attempting to get user by system_id: {system_id}")
    return db.query(User).filter(User.system_id == system_id).first()

# Example: Get a user by ID
def get_user_by_id(db: Session, user_id: int) -> Optional[User]:
    logger.debug(f"Attempting to get user by ID: {user_id}")
    return db.query(User).filter(User.id == user_id).first()


# Example: Create a new message entry
def create_message(
    db: Session,
    source_addr: str,
    destination_addr: str,
    short_message: str,
    user_id: int,
    encoding: str = None,
    client_message_id: str = None
) -> Message:
     db_message = Message(
         source_addr=source_addr,
         destination_addr=destination_addr,
         short_message=short_message,
         user_id=user_id, # ফরেন কী
         encoding=encoding,
         client_message_id=client_message_id,
         status=Message.MessageStatus.PENDING # প্রাথমিক স্ট্যাটাস PENDING
     )
     db.add(db_message)
     db.commit()
     db.refresh(db_message)
     logger.info(f"Created new message ID: {db_message.id} for user {user_id}")
     return db_message

# Example: Get a message by its Database ID
def get_message_by_id(db: Session, message_id: int) -> Optional[Message]:
    logger.debug(f"Attempting to get message by DB ID: {message_id}")
    return db.query(Message).filter(Message.id == message_id).first()

# Example: Get a message by its Vendor Message ID
# DLR হ্যান্ডলিং এর সময় Vendor Message ID দিয়ে মেসেজ খুঁজে বের করা লাগতে পারে
def get_message_by_vendor_message_id(db: Session, vendor_message_id: str) -> Optional[Message]:
    logger.debug(f"Attempting to get message by Vendor Message ID: {vendor_message_id}")
    # Vendor Message ID ইউনিক হওয়া উচিত
    return db.query(Message).filter(Message.vendor_message_id == vendor_message_id).first()

# Example: Update the status of a message
def update_message_status(
    db: Session,
    message_db_id: int, # ডাটাবেস ID
    new_status: Message.MessageStatus,
    vendor_message_id: str = None, # যদি DLR থেকে আসছে এবং Vendor Message ID জানা থাকে
    delivered_at: float = None, # ডেলিভারি টাইমস্ট্যাম্প (Unix timestamp)
    vendor_error_code: str = None # ভেন্ডর এরর কোড (যদি থাকে)
):
    # ডাটাবেস ID দিয়ে মেসেজ খুঁজে বের করুন
    message = get_message_by_id(db, message_db_id)

    if message:
        logger.info(f"Updating status for message ID {message.id} from {message.status.value} to {new_status.value}")
        message.status = new_status
        message.last_status_update_at = time.time() # শেষ আপডেটের সময়

        if new_status == Message.MessageStatus.DELIVERED and delivered_at:
            # Ensure delivered_at is a valid timestamp
            message.delivered_at = delivered_at # ডেলিভারি টাইমস্ট্যাম্প সেট করুন

        if new_status == Message.MessageStatus.FAILED and vendor_error_code:
            # vendor_error_code সেভ করার জন্য Message মডেলে ফিল্ড যোগ করতে হতে পারে (যদি না থাকে)
            logger.warning(f"Message ID {message.id} failed with vendor error code: {vendor_error_code}")
            # message.vendor_error_code = vendor_error_code # মডেলে যোগ করুন

        # vendor_message_id যদি DLR থেকে আসে এবং এখনো সেভ না হয়ে থাকে
        if vendor_message_id and message.vendor_message_id is None:
             message.vendor_message_id = vendor_message_id

        db.commit() # পরিবর্তন সেভ করুন
        # db.refresh(message) # চাইলে রিফ্রেশ করতে পারেন যদি আপডেটেড অবজেক্ট দরকার হয়
        logger.info(f"Message ID {message.id} status updated to {new_status.value}.")
        return True # সফল হলে True রিটার্ন করুন
    else:
        logger.warning(f"Message with DB ID {message_db_id} not found for status update.")
        return False # মেসেজ না পেলে False রিটার্ন করুন


# Example: Add a new vendor
def create_vendor(db: Session, name: str, protocol: str = "SMPP", **kwargs) -> Vendor:
    db_vendor = Vendor(name=name, protocol=protocol, **kwargs)
    db.add(db_vendor)
    db.commit()
    db.refresh(db_vendor)
    logger.info(f"Created new vendor: {db_vendor.name}")
    return db_vendor

# Example: Get a vendor by name
def get_vendor_by_name(db: Session, name: str) -> Optional[Vendor]:
    logger.debug(f"Attempting to get vendor by name: {name}")
    return db.query(Vendor).filter(Vendor.name == name).first()

# Example: Get active vendors
def get_active_vendors(db: Session) -> List[Vendor]:
     logger.debug("Attempting to get active vendors.")
     return db.query(Vendor).filter(Vendor.is_active == True).all()

# আপনার প্রয়োজন অনুযায়ী আরও CRUD ফাংশন এখানে যোগ করুন।
# যেমন রেট সেভ করা/খুঁজে বের করা, কাস্টমার যোগ করা/খুঁজে বের করা ইত্যাদি।