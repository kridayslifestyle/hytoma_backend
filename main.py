
import os
import hmac
import json
import hashlib
import logging
import secrets
import asyncio
import sys
from contextlib import asynccontextmanager
from dotenv import load_dotenv

from fastapi import (
    FastAPI,
    Query,
    HTTPException,
    Request
)

from fastapi.responses import PlainTextResponse

from admin_api import router as admin_router

from db import (
    initialize_database,
    enqueue_event,
    process_next_event
)




load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ],
    force=True
)

logger = logging.getLogger("hytoma")

VERIFY_TOKEN = os.getenv(
    "WHATSAPP_VERIFY_TOKEN", ""
)

META_APP_SECRET = os.getenv(
    "META_APP_SECRET", ""
)

EXPECTED_SENDER_ID = os.getenv(
    "WHATSAPP_PHONE_NUMBER_ID", ""
)


# Background event processor
async def worker_loop():

    while True:
        try:
            processed = await asyncio.to_thread(
                process_next_event
            )

            if not processed:
                await asyncio.sleep(2)

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception("Worker error")
            await asyncio.sleep(5)


# Application startup
@asynccontextmanager
async def lifespan(app: FastAPI):

    await asyncio.to_thread(
        initialize_database
    )

    worker = asyncio.create_task(
        worker_loop()
    )

    try:
        yield

    finally:
        worker.cancel()

        try:
            await worker

        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="HYTOMA WhatsApp Automation API",
    version="1.1.0",
    lifespan=lifespan
)

app.include_router(admin_router)

@app.get("/")
def home():
    return {
        "message": (
            "Welcome to HYTOMA WhatsApp Automation API"
        ),
        "status": "running"
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "HYTOMA WhatsApp Backend"
    }


# Meta webhook verification
@app.get("/webhook/whatsapp")
def verify_webhook(
    mode: str | None = Query(
        None,
        alias="hub.mode"
    ),
    token: str | None = Query(
        None,
        alias="hub.verify_token"
    ),
    challenge: str | None = Query(
        None,
        alias="hub.challenge"
    ),
):

    if (
        mode == "subscribe"
        and VERIFY_TOKEN
        and token
        and secrets.compare_digest(
            token,
            VERIFY_TOKEN
        )
        and challenge is not None
    ):
        return PlainTextResponse(
            content=challenge
        )

    raise HTTPException(
        status_code=403,
        detail="Verification failed"
    )


# Validate incoming Meta signature
def verify_signature(
    body: bytes,
    signature: str | None
) -> bool:

    if not META_APP_SECRET or not signature:
        return False

    expected = "sha256=" + hmac.new(
        META_APP_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(
        expected,
        signature
    )


# Extract supported customer reply
def extract_text(msg: dict) -> str | None:

    message_type = msg.get("type")

    if message_type == "text":
        return msg.get("text", {}).get("body")

    if message_type == "interactive":
        interactive = msg.get("interactive", {})

        return (
            interactive.get(
                "button_reply", {}
            ).get("id")

            or interactive.get(
                "list_reply", {}
            ).get("id")
        )

    if message_type == "button":
        return (
            msg.get("button", {}).get("payload")
            or msg.get("button", {}).get("text")
        )

    return None


# Receive WhatsApp messages
@app.post("/webhook/whatsapp")
async def receive_webhook(request: Request):

    body = await request.body()

    signature = request.headers.get(
        "X-Hub-Signature-256"
    )

    if not verify_signature(body, signature):
        raise HTTPException(
            status_code=403,
            detail="Invalid webhook signature"
        )

    try:
        payload = json.loads(body)

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

            value = change.get("value") or {}

            sender_id = (
                value.get("metadata") or {}
            ).get("phone_number_id")

            if (
                EXPECTED_SENDER_ID
                and sender_id
                and sender_id != EXPECTED_SENDER_ID
            ):
                continue

            for msg in value.get("messages") or []:

                message_id = msg.get("id")
                phone = msg.get("from")

                content = extract_text(msg)

                if (
                    not message_id
                    or not phone
                    or not content
                ):
                    continue

                ref = msg.get("referral") or {}

                ad_id = (
                    ref.get("source_id")
                    if ref.get("source_type") == "ad"
                    else None
                )

                await asyncio.to_thread(
                    enqueue_event,
                    message_id,
                    phone,
                    content,
                    sender_id,
                    ad_id
                )

    return {
        "status": "received"
    }
