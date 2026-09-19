"""Negotiated limits for streams carrying private artifacts."""

PRIVATE_STREAM_CAPABILITY = "private_stream_v1"
PRIVATE_EVENT_MAX_BYTES = 8 * 1024 * 1024
PRIVATE_REPLAY_MAX_BYTES = 64 * 1024 * 1024
PRIVATE_INBOX_MAX_ITEMS = 8
PRIVATE_STREAM_MAX_CONNECTIONS = 4


class ProcessingTransportError(RuntimeError):
    pass


class ProcessingContinuityError(ProcessingTransportError):
    """Delivery must remain suspended until explicit operational recovery."""
