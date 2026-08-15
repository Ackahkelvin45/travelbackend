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
    key = settings.PAYSTACK_SECRET_KEY
    if not key:
        logger.error("PAYSTACK_SECRET_KEY is missing or empty! Check your .env or .env.prod file.")

    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }


def _raise_for_paystack_error(response: requests.Response) -> dict:
    """
    Parse the Paystack JSON response, raise ValueError if status == false,
    and return the ``data`` dict on success.
    """
    try:
        response.raise_for_status()          # raises HTTPError on 4xx / 5xx
    except requests.exceptions.HTTPError as e:
        # Log the full response body to see the specific error message from Paystack
        logger.error("Paystack Error Response: %s", response.text)
        raise e

    body = response.json()
    if not body.get("status"):
        raise ValueError(body.get("message", "Paystack returned an error."))
    return body.get("data", {})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _post(path: str, payload: dict) -> dict:
    """
    POST helper with deliberate retry semantics (pattern from production
    ticketing: EnqoreAccess):

      - ConnectionError / ConnectTimeout → the request never reached Paystack,
        so no charge exists — ONE transparent retry is safe.
      - ReadTimeout → the request MAY have reached Paystack and created a live
        charge — re-POSTing risks a double charge, so never retry; the webhook
        / verify path settles the outcome.
    """
    url = f"{PAYSTACK_BASE_URL}{path}"
    try:
        response = requests.post(url, headers=_headers(), json=payload, timeout=30)
    except (requests.ConnectionError, requests.ConnectTimeout):
        logger.warning("Paystack connect failure on %s — retrying once.", path)
        response = requests.post(url, headers=_headers(), json=payload, timeout=30)
    return _raise_for_paystack_error(response)


# Ghana Mobile-Money providers, buyer-chosen (never inferred from the phone
# prefix — prefixes go stale with number portability):
#   mtn / atl  → direct POST /charge (Paystack pushes the approve-prompt)
#   vod (Telecel) → hosted checkout restricted to mobile_money (its direct
#                   charge returns send_otp, which needs Paystack's own UI)
DIRECT_MOMO_PROVIDERS = {"mtn", "atl"}
HOSTED_MOMO_PROVIDERS = {"vod"}


def normalize_msisdn(phone: str) -> str:
    """Normalise a Ghana number to local 0XXXXXXXXX (Paystack mobile_money.phone)."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if digits.startswith("233") and len(digits) == 12:
        return "0" + digits[3:]
    if len(digits) == 9:
        return "0" + digits
    return digits


def initialize_transaction(
    *,
    email: str,
    amount_kobo: int,
    reference: str,
    currency: str,          # always parsed from booking.currency — no default
    callback_url: str | None = None,
    metadata: dict | None = None,
    channels: list[str] | None = None,
) -> dict:
    """
    POST /transaction/initialize (hosted checkout / Inline popup).

    Returns Paystack's ``data`` dict which contains:
        authorization_url, access_code, reference

    ``channels`` restricts the methods Paystack shows, e.g. ["card"] or
    ["mobile_money"] — the buyer's explicit choice is authoritative.
    """
    payload: dict = {
        "email": email,
        "amount": amount_kobo,        # Must be an integer in kobo/pesewas
        "reference": reference,
        "currency": currency,
    }
    if channels:
        payload["channels"] = channels
    if callback_url:
        payload["callback_url"] = callback_url
    if metadata:
        payload["metadata"] = json.dumps(metadata)

    logger.info("Paystack initialize | payload=%s", payload)
    return _post("/transaction/initialize", payload)


def charge_mobile_money(
    *,
    email: str,
    amount_kobo: int,
    reference: str,
    currency: str,
    phone: str,
    provider: str,
    metadata: dict | None = None,
) -> dict:
    """
    POST /charge with a ``mobile_money`` object — the direct MoMo path.

    Paystack pushes the approve-prompt straight to the buyer's phone; no
    redirect, no popup. Returns immediately with data.status typically
    ``pay_offline`` (prompt sent — show data.display_text and poll) or
    ``failed``. Settlement arrives via the charge.success webhook / verify.
    """
    payload: dict = {
        "email": email,
        "amount": amount_kobo,
        "reference": reference,
        "currency": currency,
        "mobile_money": {"phone": normalize_msisdn(phone), "provider": provider},
    }
    if metadata:
        payload["metadata"] = json.dumps(metadata)

    logger.info("Paystack momo charge | ref=%s provider=%s", reference, provider)
    return _post("/charge", payload)


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
