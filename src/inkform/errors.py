class InkformError(Exception):
    """An actionable error with machine-readable diagnostics."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self):
        return {"error": self.code, "message": self.message, "details": self.details}
