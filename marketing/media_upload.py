
"""HYTOMA Marketing — Meta template media uploads."""

import os
import json
import subprocess
import tempfile
from pathlib import Path
import requests
from dotenv import load_dotenv
from io import BytesIO
from PIL import Image, UnidentifiedImageError

load_dotenv()


# HYTOMA template sample media limits.
# Conservative limits for our initial implementation.
ALLOWED_MEDIA_TYPES = {
    "image/jpeg": 5 * 1024 * 1024,
    "image/png": 5 * 1024 * 1024,
    "video/mp4": 16 * 1024 * 1024,
}


def validate_template_media(
    filename: str,
    mime_type: str,
    file_size: int,
):
    """Validate uploaded template sample media."""

    if mime_type not in ALLOWED_MEDIA_TYPES:
        raise ValueError(
            "Unsupported media type. "
            "Use JPG, PNG, or MP4."
        )

    if file_size <= 0:
        raise ValueError("File cannot be empty")

    if file_size > ALLOWED_MEDIA_TYPES[mime_type]:
        raise ValueError(
            "File exceeds the allowed size"
        )

    extension = os.path.splitext(
        filename.lower()
    )[1]

    expected_extensions = {
        "image/jpeg": {".jpg", ".jpeg"},
        "image/png": {".png"},
        "video/mp4": {".mp4"},
    }

    if extension not in expected_extensions[mime_type]:
        raise ValueError(
            "File extension doesn't match media type"
        )

    return True



def validate_mp4_video(file_data: bytes):
    """
    Validate an MP4 video using FFprobe.

    Checks:
    - MP4 container
    - H.264 video codec
    - Valid video dimensions
    - Duration between 0 and 120 seconds

    File-size validation happens separately.
    """

    if (
        len(file_data) < 12
        or file_data[4:8] != b"ftyp"
    ):
        raise ValueError("Invalid MP4 file signature")

    temporary_path = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".mp4",
            delete=False,
        ) as temporary_file:
            temporary_file.write(file_data)
            temporary_path = temporary_file.name

        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries",
                "format=format_name,duration:"
                "stream=codec_type,codec_name,width,height",
                "-of", "json",
                temporary_path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        if result.returncode != 0:
            raise ValueError(
                "Video could not be inspected"
            )

        metadata = json.loads(result.stdout)

        format_info = metadata.get("format", {})
        format_name = format_info.get(
            "format_name", ""
        )

        if "mp4" not in format_name.split(","):
            raise ValueError(
                "File is not a supported MP4 container"
            )

        duration = float(
            format_info.get("duration", 0)
        )

        if not 0 < duration <= 120:
            raise ValueError(
                "Video duration must be "
                "between 0 and 120 seconds"
            )

        video_streams = [
            stream
            for stream in metadata.get("streams", [])
            if stream.get("codec_type") == "video"
        ]

        if len(video_streams) != 1:
            raise ValueError(
                "Video must contain exactly one video stream"
            )

        video = video_streams[0]

        if video.get("codec_name") != "h264":
            raise ValueError(
                "Only H.264 encoded MP4 videos are supported"
            )

        width = int(video.get("width", 0))
        height = int(video.get("height", 0))

        if not (
            0 < width <= 1920
            and 0 < height <= 1920
        ):
            raise ValueError(
                "Unsupported video dimensions"
            )

        return True

    except (
        subprocess.TimeoutExpired,
        FileNotFoundError,
        OSError,
        json.JSONDecodeError,
        TypeError,
        OverflowError,
    ) as exc:
        raise ValueError(
            "Unable to validate MP4 video"
        ) from exc

    finally:
        if temporary_path is not None:
            Path(temporary_path).unlink(
                missing_ok=True
            )





def validate_media_content(
    filename: str,
    mime_type: str,
    file_data: bytes,
):
    """
    Validate template sample media.

    Images are decoded and verified with Pillow.
    MP4 files require a separate video validator.
    """

    validate_template_media(
        filename=filename,
        mime_type=mime_type,
        file_size=len(file_data),
    )

    if mime_type == "video/mp4":
        return validate_mp4_video(file_data)

    expected_format = {
        "image/jpeg": "JPEG",
        "image/png": "PNG",
    }.get(mime_type)

    if expected_format is None:
        raise ValueError("Unsupported image format")

    try:
        # Verify image structure.
        with Image.open(BytesIO(file_data)) as image:
            if image.format != expected_format:
                raise ValueError(
                    "Image format does not match MIME type"
                )

            image.verify()

        # Reopen and actually decode pixel data.
        with Image.open(BytesIO(file_data)) as image:
            image.load()

            if image.format != expected_format:
                raise ValueError(
                    "Unexpected image format"
                )

    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
    ) as exc:
        raise ValueError(
            "Invalid or corrupted image file"
        ) from exc

    return True




def create_upload_session(
    file_size: int,
    mime_type: str,
    filename: str,
):
    """Create a Meta resumable upload session."""

    token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    app_id = os.getenv("META_APP_ID")
    version = os.getenv(
        "WHATSAPP_API_VERSION", "v27.0"
    )

    if not token or not app_id:
        raise RuntimeError(
            "Meta access token or META_APP_ID missing"
        )

    validate_template_media(
        filename=filename,
        mime_type=mime_type,
        file_size=file_size,
    )

    # Never accept an arbitrary filename as a URL/path.
    safe_filename = os.path.basename(
        filename.replace("\\", "/")
    )

    url = (
        f"https://graph.facebook.com/"
        f"{version}/{app_id}/uploads"
    )

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
        },
        params={
            "file_name": safe_filename,
            "file_length": file_size,
            "file_type": mime_type,
        },
        timeout=30,
    )

    response.raise_for_status()

    return response.json()["id"]



def upload_template_media(
    session_id: str,
    file_data: bytes,
):
    """Upload sample media and return Meta's file handle."""

    token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    version = os.getenv(
        "WHATSAPP_API_VERSION", "v27.0"
    )

    if not token:
        raise RuntimeError(
            "WhatsApp access token missing"
        )

    url = (
        f"https://graph.facebook.com/"
        f"{version}/{session_id}"
    )

    response = requests.post(
        url,
        headers={
            "Authorization": f"OAuth {token}",
            "file_offset": "0",
            "Content-Type": "application/octet-stream",
        },
        data=file_data,
        timeout=60,
    )

    response.raise_for_status()
    return response.json()["h"]
