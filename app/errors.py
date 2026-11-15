class AppError(Exception):
    """An error with an HTTP status and an i18n message key (see app.i18n)."""

    def __init__(self, status: int, key: str):
        super().__init__(key)
        self.status = status
        self.key = key


class ClientGone(Exception):
    """The caller hung up before the answer was ready (e.g. the UI moved on to another song)."""
