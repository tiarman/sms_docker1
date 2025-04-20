import os
from typing import Dict, Any

# from dotenv import load_dotenv
# load_dotenv()

def load_config() -> Dict[str, Any]:
    config = {
        "server": {
            "host": os.getenv("SMPP_HOST", "0.0.0.0"),
            "port": int(os.getenv("SMPP_PORT", "2775")),
            "system_id": os.getenv("SMPP_SYSTEM_ID", "SMPP_SRV"),
            "password": os.getenv("SMPP_PASSWORD", "secret"),
            "system_type": os.getenv("SMPP_SYSTEM_TYPE", ""),
            "version": float(os.getenv("SMPP_VERSION", "3.4")),
            "max_connections": int(os.getenv("SMPP_MAX_CONNECTIONS", "100")),
            "timeout": int(os.getenv("SMPP_TIMEOUT", "30")),
            "tps_limit": int(os.getenv("SMPP_TPS_LIMIT", "600")),
        },
        "database": {
            "host": os.getenv("DB_HOST", "localhost"),
            "port": int(os.getenv("DB_PORT", "5432")),
            "name": os.getenv("DB_NAME", "smppdb"),
            "user": os.getenv("DB_USER", "smppuser"),
            "password": os.getenv("DB_PASSWORD", "smpppassword"),
        },
        "redis": {
            "host": os.getenv("REDIS_HOST", "localhost"),
            "port": int(os.getenv("REDIS_PORT", "6379")),
            "db": int(os.getenv("REDIS_DB", "0")),
        },
        "rabbitmq": {
            "host": os.getenv("RABBITMQ_HOST", "localhost"),
            "port": int(os.getenv("RABBITMQ_PORT", "5672")),
            "user": os.getenv("RABBITMQ_USER", "smppuser"),
            "password": os.getenv("RABBITMQ_PASSWORD", "smpppassword"),
            "exchange": "smpp_messages",
        },
        "logging": {
            "level": os.getenv("LOG_LEVEL", "INFO"),
            "file": os.getenv("LOG_FILE", "/var/log/smpp-server/server.log"),
        },
    }

    config_file = os.getenv("CONFIG_FILE", "/etc/smpp-server/smpp-server.conf")
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        try:
                            key, value = line.split('=', 1)
                            key = key.strip()
                            value = value.strip()
                            if '.' in key:
                                section, param = key.split('.', 1)
                                if section in config and param in config[section]:
                                    if isinstance(config[section][param], int):
                                        config[section][param] = int(value)
                                    elif isinstance(config[section][param], float):
                                        config[section][param] = float(value)
                                    else:
                                        config[section][param] = value
                        except ValueError:
                            pass
        except Exception as e:
             import logging
             logging.error(f"Error loading config file {config_file}: {e}")

    return config