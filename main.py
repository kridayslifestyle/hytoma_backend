
import os
import secrets

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

app = FastAPI(
    title="HYTOMA WhatsApp Automation API",
    description="WhatsApp Lead Qualification Backend",
    version="1.0.0"
)

VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")


# Homepage
@app.get("/")
def home():
    return {
        "message": "Welcome to HYTOMA WhatsApp Automation API",
        "status": "running"
    }


# Health check
@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "HYTOMA WhatsApp Backend"
    }


# Meta WhatsApp webhook verification
@app.get("/webhook/whatsapp")
def verify_webhook(
    mode: str = Query(None, alias="hub.mode"),
    token: str = Query(None, alias="hub.verify_token"),
    challenge: str = Query(None, alias="hub.challenge"),
):
    if (
        mode == "subscribe"
        and VERIFY_TOKEN
        and token
        and secrets.compare_digest(token, VERIFY_TOKEN)
        and challenge is not None
    ):
        return PlainTextResponse(content=challenge)

    raise HTTPException(
        status_code=403,
        detail="Verification failed"
    )
