"""Versioned data contracts for CareThread."""

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Base Pydantic v2 model forbidding undeclared fields."""

    model_config = ConfigDict(extra="forbid")


__all__ = ["StrictModel"]
