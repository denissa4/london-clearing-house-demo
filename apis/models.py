"""Pydantic response models for the PoC APIs (drive the OpenAPI docs)."""
from typing import Optional

from pydantic import BaseModel, Field


class RateInfo(BaseModel):
    rate_id: str
    name: str
    currency: str
    source: str


class RateObservation(BaseModel):
    rate_id: str
    name: str
    currency: str
    source: str
    date: str
    value: Optional[float] = None
    volume_billions: Optional[float] = None


class RateHistory(BaseModel):
    rate_id: str
    name: str
    currency: str
    source: str
    observations: list[dict] = Field(default_factory=list)


class Address(BaseModel):
    country: Optional[str] = None
    city: Optional[str] = None
    postal_code: Optional[str] = None


class Entity(BaseModel):
    lei: Optional[str] = None
    name: Optional[str] = None
    status: Optional[str] = None
    legal_form: Optional[str] = None
    jurisdiction: Optional[str] = None
    legal_address: Address = Field(default_factory=Address)
    registration_status: Optional[str] = None


class EntitySearch(BaseModel):
    query: str
    count: int
    results: list[Entity] = Field(default_factory=list)


class EntityRelationships(BaseModel):
    lei: str
    direct_parent: Optional[Entity] = None
    ultimate_parent: Optional[Entity] = None
