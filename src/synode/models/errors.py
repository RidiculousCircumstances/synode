from __future__ import annotations


class ModelError(RuntimeError):
    """Base class for model provider failures."""


class ModelProviderUnavailableError(ModelError):
    """Raised when a configured provider cannot be reached or used."""


class ModelResponseError(ModelError):
    """Raised when a provider returns an invalid response envelope."""


class StructuredOutputValidationError(ModelError):
    """Raised when structured model output does not match the requested schema."""

