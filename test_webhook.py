
import os
import json
import hmac
import hashlib
import urllib.request

from dotenv import load_dotenv

load_dotenv()

secret = os.environ["META_APP_SECRET"]

payload = {
    "object": "whatsapp_business_account",
    "entry": [{
        "changes": [{
            "field": "messages",
            "value": {
                "messages": [{
                    "id": "wamid.LOCAL_TEST_001",
                    "from": "919876543210",
                    "type": "text",
                    "text": {
                        "body": "Hi HYTOMA"
                    }
                }]
            }
        }]
    }]
}

body = json.dumps(payload).encode("utf-8")

signature = "sha256=" + hmac.new(
    secret.encode("utf-8"),
    body,
    hashlib.sha256
).hexdigest()

req = urllib.request.Request(
    "http://127.0.0.1:8000/webhook/whatsapp",
    data=body,
    headers={
        "Content-Type": "application/json",
        "X-Hub-Signature-256": signature
    },
    method="POST"
)

with urllib.request.urlopen(req) as response:
    print(response.read().decode())
