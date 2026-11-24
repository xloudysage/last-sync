class ServiceError(Exception):
    """Safe user-facing error. Never includes URLs, tokens or response bodies."""

    def __init__(self, service, message, *, status=None, retry_after=0,
                 fatal=False, uncertain=False):
        super().__init__(message)
        self.service = service
        self.status = status
        self.retry_after = max(0, retry_after)
        self.fatal = fatal
        self.uncertain = uncertain


class RetryPolicy:
    def __init__(self):
        self.failures = {}

    def success(self, service):
        self.failures.pop(service, None)

    def failure(self, error):
        n = min(self.failures.get(error.service, 0) + 1, 6)
        self.failures[error.service] = n
        return max(error.retry_after, min(60, 2 ** n))
