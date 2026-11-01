class LastfmError(Exception):
    pass


class LastfmRequestError(LastfmError):
    def __init__(self, state, message, code=None):
        self.state, self.code = state, code
        super().__init__(message)
