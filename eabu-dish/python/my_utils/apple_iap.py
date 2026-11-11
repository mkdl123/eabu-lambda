"""Apple in-app purchase checks for paid ads (the $9.99 / 3-month auto-renewing subscription).

Only the functions that take payments import this module, and only they list
app-store-server-library in their requirements, so the other functions stay small.

The app buys the subscription with StoreKit and passes `appAccountToken`, a UUID the
backend derives from the signed-in user (`account_token`). Every signed transaction
or notification is verified against Apple's root certificates (bundled next to this
file) before anything is trusted, and the token ties a purchase to the Eabu user who
made it.
"""
import os
import uuid
from datetime import datetime, timezone

CERT_DIR = os.path.join(os.path.dirname(__file__), "apple_certs")
# A fixed namespace, so the same user always gets the same token. Never change it:
# existing subscriptions carry tokens made from it.
_TOKEN_NAMESPACE = uuid.UUID("6f1c9a52-4d8e-4b7a-9f3e-2c5d8a1b7e40")

PAID_STATUS = "active"        # the status the public ad list shows
AWAITING_STATUS = "awaiting_payment"
EXPIRED_STATUS = "expired"


class PurchaseError(ValueError):
    """A purchase or notification that failed verification or doesn't belong here."""


def bundle_id():
    return os.environ.get("APP_BUNDLE_ID", "net.eabu.app")


def product_id():
    return os.environ.get("AD_PRODUCT_ID", "net.eabu.app.ad.quarterly")


def account_token(user_id):
    """The appAccountToken for a user: a stable UUID, so no user id goes to Apple."""
    return str(uuid.uuid5(_TOKEN_NAMESPACE, str(user_id)))


def _root_certs():
    certs = []
    for name in sorted(os.listdir(CERT_DIR)):
        if name.endswith(".cer"):
            with open(os.path.join(CERT_DIR, name), "rb") as f:
                certs.append(f.read())
    return certs


_verifiers = {}


def _verifier(environment):
    """One verifier per environment, reused while the container is warm."""
    if environment not in _verifiers:
        from appstoreserverlibrary.models.Environment import Environment
        from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier
        env = Environment.PRODUCTION if environment == "Production" else Environment.SANDBOX
        app_id = os.environ.get("APP_APPLE_ID", "").strip()
        _verifiers[environment] = SignedDataVerifier(
            _root_certs(), True, env, bundle_id(),
            int(app_id) if environment == "Production" and app_id.isdigit() else None)
    return _verifiers[environment]


def _environments():
    """Production first; Sandbox (TestFlight and Xcode builds) unless turned off.

    Production purchases can only be verified once APP_APPLE_ID is set."""
    envs = ["Production"] if os.environ.get("APP_APPLE_ID", "").strip().isdigit() else []
    if os.environ.get("IAP_ALLOW_SANDBOX", "true").lower() == "true":
        envs.append("Sandbox")
    return envs


def _try_each(method, signed):
    from appstoreserverlibrary.signed_data_verifier import VerificationException
    last = "no App Store environment is configured"
    for environment in _environments():
        try:
            return getattr(_verifier(environment), method)(signed), environment
        except VerificationException as e:
            last = e
    raise PurchaseError(f"verification failed: {getattr(last, 'status', last)}")


def verify_transaction(signed_transaction):
    """Verified transaction details as a plain dict, or PurchaseError."""
    tx, environment = _try_each("verify_and_decode_signed_transaction", signed_transaction)
    return _transaction_dict(tx, environment)


def verify_renewal_info(signed_renewal_info):
    info, _ = _try_each("verify_and_decode_renewal_info", signed_renewal_info)
    return {"autoRenew": getattr(info, "rawAutoRenewStatus", None) == 1,
            "originalTransactionId": info.originalTransactionId}


def verify_notification(signed_payload):
    """A verified App Store Server Notification (V2) as a plain dict, or PurchaseError."""
    note, environment = _try_each("verify_and_decode_notification", signed_payload)
    data = note.data
    out = {"type": note.rawNotificationType, "subtype": note.rawSubtype,
           "uuid": note.notificationUUID, "environment": environment,
           "transaction": None, "renewal": None}
    if data is not None and data.signedTransactionInfo:
        out["transaction"] = verify_transaction(data.signedTransactionInfo)
    if data is not None and data.signedRenewalInfo:
        out["renewal"] = verify_renewal_info(data.signedRenewalInfo)
    return out


def _transaction_dict(tx, environment):
    return {
        "originalTransactionId": tx.originalTransactionId,
        "transactionId": tx.transactionId,
        "bundleId": tx.bundleId,
        "productId": tx.productId,
        "appAccountToken": (tx.appAccountToken or "").lower(),
        "expiresAt": int((tx.expiresDate or 0) / 1000),   # epoch seconds
        "revoked": tx.revocationDate is not None,
        "environment": environment,
    }


def check_ad_purchase(tx, user_id, now=None):
    """Raise PurchaseError unless tx is a live ad subscription bought by user_id."""
    now = now or int(datetime.now(timezone.utc).timestamp())
    if tx["bundleId"] != bundle_id() or tx["productId"] != product_id():
        raise PurchaseError("not an Eabu ad subscription")
    if tx["appAccountToken"] != account_token(user_id):
        raise PurchaseError("purchase belongs to another account")
    if tx["revoked"]:
        raise PurchaseError("purchase was refunded or revoked")
    if tx["expiresAt"] <= now:
        raise PurchaseError("subscription has expired")
