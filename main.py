
import os
import hmac
import json
import hashlib
import logging
import secrets

from dotenv import load_dotenv
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import PlainTextResponse

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hytoma")

app = FastAPI(
    title="HYTOMA WhatsApp Automation API",
    version="1.0.0"
)

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")


@app.get("/")
def home():
    return {
        "message": "Welcome to HYTOMA WhatsApp Automation API",
        "status": "running"
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "HYTOMA WhatsApp Backend"
    }


# Meta GET webhook verification
@app.get("/webhook/whatsapp")
def verify_webhook(
    mode: str | None = Query(None, alias="hub.mode"),
    token: str | None = Query(None, alias="hub.verify_token"),
    challenge: str | None = Query(None, alias="hub.challenge")
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


# Check that Meta signed the incoming request
def verify_signature(body: bytes, signature: str | None):
    if not META_APP_SECRET or not signature:
        return False

    expected = "sha256=" + hmac.new(
        META_APP_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


# Receive incoming WhatsApp events
@app.post("/webhook/whatsapp")
async def receive_whatsapp_webhook(request: Request):

    raw_body = await request.body()

    signature = request.headers.get(
        "X-Hub-Signature-256"
    )

    if not verify_signature(raw_body, signature):
        raise HTTPException(
            status_code=403,
            detail="Invalid webhook signature"
        )

    try:
        payload = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON payload"
        )

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="Expected JSON object"
        )

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):

            if change.get("field") != "messages":
                continue

            value = change.get("value", {})

            # Customer messages
            for message in value.get("messages", []):
                phone = message.get("from", "")
                message_id = message.get("id")
                message_type = message.get("type")

                logger.info(
                    "Incoming WhatsApp message: id=%s type=%s sender_last4=%s",
                    message_id,
                    message_type,
                    phone[-4:]
                )

            # Delivery/read status events
            for status in value.get("statuses", []):
                logger.info(
                    "WhatsApp status: id=%s status=%s",
                    status.get("id"),
                    status.get("status")
                )

    return {"status": "received"}
