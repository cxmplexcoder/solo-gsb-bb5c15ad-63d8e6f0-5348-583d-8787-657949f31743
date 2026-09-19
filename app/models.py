"""Request/response schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class MergeCreate(BaseModel):
    base: Any = Field(..., description="Common ancestor JSON document")
    left: Any = Field(..., description="Left side JSON document")
    right: Any = Field(..., description="Right side JSON document")
    idempotency_key: str | None = Field(
        default=None,
        description="Optional key; may also be sent as the Idempotency-Key header",
    )


class ResolutionItem(BaseModel):
    pointer: str = Field(..., description="JSON Pointer of an open conflict")
    choice: Literal["left", "right"] | None = Field(
        default=None, description="Take the candidate from this side"
    )
    value: Any = Field(
        default=None, description="Explicit replacement value (may be null)"
    )

    @property
    def value_provided(self) -> bool:
        return "value" in self.model_fields_set


class ResolveRequest(BaseModel):
    revision: int = Field(..., description="Current merge revision (409 if stale)")
    resolutions: list[ResolutionItem] = Field(
        ..., min_length=1, description="Batch of conflict resolutions"
    )


class RuleItem(BaseModel):
    pattern: str = Field(
        ...,
        description=(
            "RFC 6901-style pointer pattern; '*' matches one segment and a "
            "trailing '**' matches zero or more remaining segments"
        ),
    )
    choice: Literal["left", "right"] = Field(
        ..., description="Side every conflict matched by this rule takes"
    )


class RuleResolutionRequest(BaseModel):
    revision: int = Field(..., description="Current merge revision (409 if stale)")
    rules: list[RuleItem] = Field(
        ..., min_length=1, description="Ordered rules; earlier declarations win ties"
    )
