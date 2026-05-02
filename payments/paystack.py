"""
payments/paystack.py
--------------------
Thin service layer for the Paystack Transactions API.

All outbound HTTP calls to Paystack live here so that:
  - Views stay thin (no requests.* in views).
  - This module is easy to mock in tests.
  - Any future SDK swap is a one-file change.
"""

import hashlib
import hmac
import json
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

PAYSTACK_BASE_URL = "https://api.paystack.co"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    """Build the authorization header for every Paystack request."""
    return {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }


def _raise_for_paystack_error(response: requests.Response) -> dict:
    """
    Parse the Paystack JSON response, raise ValueError if status == false,
    and return the ``data`` dict on success.
    """
    response.raise_for_status()          # raises HTTPError on 4xx / 5xx
    body = response.json()
    if not body.get("status"):
        raise ValueError(body.get("message", "Paystack returned an error."))
    return body.get("data", {})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def initialize_transaction(
    *,
    email: str,
    amount_kobo: int,
    reference: str,
    currency: str,          # always parsed from booking.currency — no default
    callback_url: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """
    POST /transaction/initialize

    Returns Paystack's ``data`` dict which contains:
        authorization_url, access_code, reference
    """
    payload: dict = {
        "email": email,
        "amount": str(amount_kobo),   # must be a string in kobo/pesewas
        "reference": reference,
        "currency": currency,
    }
    if callback_url:
        payload["callback_url"] = callback_url
    if metadata:
        payload["metadata"] = json.dumps(metadata)

    logger.info("Paystack initialize | ref=%s email=%s amount=%s", reference, email, amount_kobo)

    response = requests.post(
        f"{PAYSTACK_BASE_URL}/transaction/initialize",
        headers=_headers(),
        json=payload,
        timeout=30,
    )
    return _raise_for_paystack_error(response)


def verify_transaction(reference: str) -> dict:
    """
    GET /transaction/verify/:reference

    Returns Paystack's ``data`` dict which contains:
        status, amount, channel, paid_at, authorization, customer, …
    """
    logger.info("Paystack verify | ref=%s", reference)

    response = requests.get(
        f"{PAYSTACK_BASE_URL}/transaction/verify/{reference}",
        headers=_headers(),
        timeout=30,
    )
    return _raise_for_paystack_error(response)


def verify_webhook_signature(payload_bytes: bytes, signature: str) -> bool:
    """
    Verify the HMAC-SHA512 signature Paystack attaches to every webhook.

    Paystack signs the raw request body with your secret key and sends the
    hex digest in the ``X-Paystack-Signature`` header.

    Returns True only if the computed digest matches the header value.
    """
    secret = settings.PAYSTACK_SECRET_KEY.encode("utf-8")
    computed = hmac.new(secret, payload_bytes, hashlib.sha512).hexdigest()
    return hmac.compare_digest(computed, signature)
