from __future__ import annotations


class CeaAIError(RuntimeError):
    pass


class NotFoundError(CeaAIError):
    pass


class BusinessRuleError(CeaAIError):
    def __init__(self, message: str, *, generation_id: int | None = None) -> None:
        super().__init__(message)
        self.generation_id = generation_id


class NoActiveSubscriptionError(BusinessRuleError):
    pass


class InsufficientCoinsError(BusinessRuleError):
    pass


class GenerationProviderFailedError(BusinessRuleError):
    pass
