
import os

from dotenv import load_dotenv
from whatsapp_service import (
    send_hello_world_template,
    send_whatsapp_text
)

load_dotenv()

phone = os.getenv("TEST_RECIPIENT_PHONE")

if not phone:
    raise RuntimeError("TEST_RECIPIENT_PHONE missing")

print("HYTOMA WhatsApp API Test")
print("1. Send hello_world template")
print("2. Send custom text message")

choice = input("Select option (1 or 2): ").strip()

try:
    if choice == "1":
        result = send_hello_world_template(phone)

    elif choice == "2":
        message = (
            "Hello! Welcome to HYTOMA Automation.\n\n"
            "This is a test message from our "
            "Python FastAPI backend."
        )

        result = send_whatsapp_text(phone, message)

    else:
        raise ValueError("Invalid selection")

    print("API request successful!")
    print("Message ID:", result.get("messages", [{}])[0].get("id"))

except Exception as error:
    print("Message sending failed:", error)
