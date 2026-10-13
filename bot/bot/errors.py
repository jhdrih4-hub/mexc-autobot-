class TradingSystemError(Exception):
    def __init__(self, message, user_message=None):
        super().__init__(message)
        self.user_message = user_message or message


class RetryableError(TradingSystemError):
    def __init__(self, message, backoff=2.0):
        super().__init__(message)
        self.backoff = backoff


class FatalError(TradingSystemError):
    pass


class TradeBlockedError(TradingSystemError):
    pass


class InsufficientDataError(TradingSystemError):
    pass


class DataQualityError(TradingSystemError):
    pass


class FeatureNotSupportedError(TradingSystemError):
    pass
