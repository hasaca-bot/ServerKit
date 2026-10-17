"""Tool-level failure: the message is shown to the MCP caller as an error result."""


class ToolError(Exception):
    """A request that is wrong or not allowed. Never carries secrets."""

    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details
