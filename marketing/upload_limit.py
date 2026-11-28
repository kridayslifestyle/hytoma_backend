"""HYTOMA Marketing — upload request size protection."""

MAX_UPLOAD_REQUEST_BYTES = 17 * 1024 * 1024

UPLOAD_PATH = "/admin/marketing/media/upload"

import re


class MediaUploadSizeLimitMiddleware:
    """
    Enforce a request-body limit before multipart parsing.

    Applies only to the marketing media-upload endpoint.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or not (
                scope.get("path") == UPLOAD_PATH
                or re.fullmatch(
                    r"/admin/campaigns/[0-9]+/media",
                    scope.get("path", ""),
                )
            )
            or scope.get("method") != "POST"
            or re.fullmatch(
                r"/admin/marketing-inbox/conversations/[0-9]+/send",
                scope.get("path", ""),
            )
        ):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")

        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                await self.reject(send, 400, "Invalid request size")
                return

            if declared_size > MAX_UPLOAD_REQUEST_BYTES:
                await self.reject(send, 413, "Upload too large")
                return

        body = bytearray()

        while True:
            message = await receive()

            if message["type"] == "http.disconnect":
                return

            if message["type"] != "http.request":
                continue

            chunk = message.get("body", b"")

            if len(body) + len(chunk) > MAX_UPLOAD_REQUEST_BYTES:
                await self.reject(send, 413, "Upload too large")
                return

            body.extend(chunk)

            if not message.get("more_body", False):
                break

        replayed = False

        async def limited_receive():
            nonlocal replayed

            if not replayed:
                replayed = True

                return {
                    "type": "http.request",
                    "body": bytes(body),
                    "more_body": False,
                }

            return await receive()

        await self.app(scope, limited_receive, send)

    @staticmethod
    async def reject(send, status, message):
        data = message.encode("utf-8")

        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(data)).encode()),
                ],
            }
        )

        await send(
            {
                "type": "http.response.body",
                "body": data,
            }
        )
