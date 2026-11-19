"""Interakt WhatsApp template send (port of the old lib/whatsapp.ts).

Configure INTERAKT_API_KEY (Interakt's Basic auth key) and, when more than one WABA number is in use,
INTERAKT_FROM. Templates must be approved in Interakt; `template` is the template name.
"""
from __future__ import annotations

import re

import requests

from .. import config

URL = "https://api.interakt.ai/v1/public/message/"
TIMEOUT = 30


def whatsapp_enabled() -> bool:
    return bool(config.INTERAKT_API_KEY)


def send_template(phone_e164: str, template: str, body_values: list[str], language_code: str = "en") -> str:
    """Send one approved template message. Raises when unconfigured or on a non-2xx response."""
    if not whatsapp_enabled():
        raise RuntimeError("WhatsApp not configured (INTERAKT_API_KEY)")
    digits = re.sub(r"\D", "", phone_e164)
    body = {
        "countryCode": "+" + digits[:-10] if len(digits) > 10 else "+91",
        "phoneNumber": digits[-10:],
        "callbackData": "calling-console",
        "type": "Template",
        "template": {"name": template, "languageCode": language_code, "bodyValues": list(body_values)},
    }
    if config.INTERAKT_FROM:
        body["from"] = config.INTERAKT_FROM
    res = requests.post(URL, json=body, headers={"Authorization": f"Basic {config.INTERAKT_API_KEY}"},
                        timeout=TIMEOUT)
    if not 200 <= res.status_code < 300:
        raise RuntimeError(f"Interakt {res.status_code}: {res.text[:200]}")
    return res.text
