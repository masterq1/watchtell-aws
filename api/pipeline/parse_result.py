"""
Step Functions Lambda: parse and normalise the Rekognition ALPR result.
Input: raw result dict from rekognition_alpr.
Output: enriched event dict ready for validation.
"""
import re


_PLATE_RE = re.compile(r"^[A-Z0-9 \-]{2,10}$")


def handler(event: dict, context) -> dict:
    plate_raw = (event.get("plate_number") or "UNKNOWN").upper().strip()

    # Normalise common OCR artifacts
    plate = plate_raw.replace("O", "0") if plate_raw.startswith("0") else plate_raw
    plate = re.sub(r"[^A-Z0-9]", "", plate)

    valid_format = bool(_PLATE_RE.match(plate_raw))

    return {
        **event,
        "plate_number": plate,
        "plate_raw": plate_raw,
        "plate_format_valid": valid_format,
        "confidence": float(event.get("confidence", 0.0)),
    }
