import logging
from fastapi import FastAPI, HTTPException

# from typing import TYPE_CHECKING
# if TYPE_CHECKING:
#     from smpp_server.server import SMPPServer

logger = logging.getLogger(__name__)

def create_api(server=None, config=None) -> FastAPI:
    logger.info("API creator placeholder called. Creating FastAPI app.")
    app = FastAPI()

    @app.get("/health")
    async def health_check():
        return {"status": "ok", "message": "SMPP Server API placeholder is running"}

    logger.info("API placeholder endpoints defined.")
    return app