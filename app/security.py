import hashlib
import hmac


def verify_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify GitHub's X-Hub-Signature-256 header against the raw request bytes.

    Must be checked against the exact bytes GitHub sent -- never a re-serialized
    parse of the body, which would silently break verification.
    """
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
