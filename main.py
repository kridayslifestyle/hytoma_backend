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
from auth import router as auth_router
from inbox_api import router as inbox_router
from fastapi import FastAPI, Query, HTTPException, Request

from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from sales_management_api import router as sales_management_router
from admin_api import router as admin_router
from safe_reply_api import router as safe_reply_router
from marketing.upload_limit import MediaUploadSizeLimitMiddleware
from marketing.api import router as marketing_router
from delivery_status import record_delivery_statuses
from delivery_status_api import router as delivery_status_router
from db import (
    initialize_database,
    enqueue_event,
    process_next_event,
    process_next_bot_outbound,
    mark_stale_bot_outbounds_unknown,
)
from contacts.api import (
    router as contacts_router,
    initialize_contacts,
)
from contacts.csv_import import router as contacts_csv_router

from contacts.consent import router as contacts_consent_router

from campaigns.api import (
    router as campaigns_router,
    initialize_campaigns,
)

from campaigns.content import (
    router as campaign_content_router,
    initialize_campaign_content,
)

from campaigns.send_test import (
    router as campaign_test_send_router,
    initialize_campaign_test_send,
)

from campaigns.bulk import (
    router as campaign_queue_router,
    initialize_campaign_queue,
    campaign_worker_loop,
)

from campaigns.reports import router as campaign_reports_router

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)

logger = logging.getLogger("hytoma")

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")

META_APP_SECRET = os.getenv("META_APP_SECRET", "")

EXPECTED_SENDER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")


# Background event processor


async def worker_loop():
    loop = asyncio.get_running_loop()
    next_recovery_check = 0.0

    while True:
        try:
            # Check abandoned sends on startup
            # and every five minutes thereafter.
            now = loop.time()

            if now >= next_recovery_check:
                recovered = await asyncio.to_thread(mark_stale_bot_outbounds_unknown)

                if recovered:
                    logger.warning(
                        "%s abandoned bot jobs require review",
                        recovered,
                    )

                next_recovery_check = loop.time() + 300

            inbound_processed = await asyncio.to_thread(process_next_event)

            outbound_processed = await asyncio.to_thread(process_next_bot_outbound)

            if not inbound_processed and not outbound_processed:
                await asyncio.sleep(2)

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception("Worker error")
            await asyncio.sleep(5)


# Application startup
@asynccontextmanager
async def lifespan(app: FastAPI):

    await asyncio.to_thread(initialize_database)

    await asyncio.to_thread(initialize_contacts)

    await asyncio.to_thread(initialize_campaigns)

    await asyncio.to_thread(initialize_campaign_content)

    await asyncio.to_thread(initialize_campaign_test_send)

    await asyncio.to_thread(initialize_campaign_queue)

    worker = asyncio.create_task(worker_loop())

    campaign_worker = asyncio.create_task(campaign_worker_loop())

    try:
        yield

    finally:

        worker.cancel()

        campaign_worker.cancel()

    try:

        await worker

    except asyncio.CancelledError:

        pass

    try:

        await campaign_worker

    except asyncio.CancelledError:

        pass


app = FastAPI(
    title="HYTOMA WhatsApp Automation API", version="1.1.0", lifespan=lifespan
)


allowed_origins = [
    origin.strip().rstrip("/")
    for origin in os.getenv(
        "ADMIN_ALLOWED_ORIGINS",
        "http://localhost:5173",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)

app.add_middleware(MediaUploadSizeLimitMiddleware)


app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(sales_management_router)
app.include_router(inbox_router)
app.include_router(safe_reply_router)
app.include_router(delivery_status_router)
app.include_router(marketing_router)
app.include_router(contacts_router)
app.include_router(contacts_csv_router)
app.include_router(contacts_consent_router)
app.include_router(campaigns_router)
app.include_router(campaign_content_router)
app.include_router(campaign_test_send_router)
app.include_router(campaign_queue_router)
app.include_router(campaign_reports_router)

@app.get("/")
def home():
    return {
        "message": ("Welcome to HYTOMA WhatsApp Automation API"),
        "status": "running",
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "HYTOMA WhatsApp Backend"}


# Meta webhook verification
@app.get("/webhook/whatsapp")
def verify_webhook(
    mode: str | None = Query(None, alias="hub.mode"),
    token: str | None = Query(None, alias="hub.verify_token"),
    challenge: str | None = Query(None, alias="hub.challenge"),
):

    if (
        mode == "subscribe"
        and VERIFY_TOKEN
        and token
        and secrets.compare_digest(token, VERIFY_TOKEN)
        and challenge is not None
    ):
        return PlainTextResponse(content=challenge)

    raise HTTPException(status_code=403, detail="Verification failed")


# Validate incoming Meta signature
def verify_signature(body: bytes, signature: str | None) -> bool:

    if not META_APP_SECRET or not signature:
        return False

    expected = (
        "sha256="
        + hmac.new(META_APP_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    )

    return hmac.compare_digest(expected, signature)


# Extract supported customer reply
def extract_text(msg: dict) -> str | None:

    message_type = msg.get("type")

    if message_type == "text":
        return msg.get("text", {}).get("body")

    if message_type == "interactive":
        interactive = msg.get("interactive", {})

        return interactive.get("button_reply", {}).get("id") or interactive.get(
            "list_reply", {}
        ).get("id")

    if message_type == "button":
        return msg.get("button", {}).get("payload") or msg.get("button", {}).get("text")

    return None


def extract_display_text(msg: dict) -> str | None:
    """
    Extract customer-friendly WhatsApp answers
    for the admin conversation history.
    """

    message_type = msg.get("type")

    if message_type == "text":
        return (msg.get("text") or {}).get("body")

    if message_type == "interactive":
        interactive = msg.get("interactive") or {}

        button = interactive.get("button_reply") or {}
        selected_list = interactive.get("list_reply") or {}

        return (
            button.get("title")
            or selected_list.get("title")
            or button.get("id")
            or selected_list.get("id")
        )

    if message_type == "button":
        button = msg.get("button") or {}

        return button.get("text") or button.get("payload")

    return None


# Receive WhatsApp messages
@app.post("/webhook/whatsapp")
async def receive_webhook(request: Request):

    body = await request.body()

    signature = request.headers.get("X-Hub-Signature-256")

    if not verify_signature(body, signature):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")

    try:
        payload = json.loads(body)

    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object")
    await asyncio.to_thread(
        record_delivery_statuses,
        payload,
    )

    for entry in payload.get("entry", []):

        for change in entry.get("changes", []):

            if change.get("field") != "messages":
                continue

            value = change.get("value") or {}

            sender_id = (value.get("metadata") or {}).get("phone_number_id")

            if not EXPECTED_SENDER_ID or sender_id != EXPECTED_SENDER_ID:
                logger.warning("Ignoring webhook event with unexpected sender ID")
                continue

            for msg in value.get("messages") or []:

                message_id = msg.get("id")
                phone = msg.get("from")

                content = extract_text(msg)
                display_content = extract_display_text(msg) or content

                if not message_id or not phone or not content:
                    continue

                ref = msg.get("referral") or {}

                ad_id = ref.get("source_id") if ref.get("source_type") == "ad" else None

                await asyncio.to_thread(
                    enqueue_event,
                    message_id,
                    phone,
                    content,
                    sender_id,
                    ad_id,
                    display_content,
                )

    return {"status": "received"}
