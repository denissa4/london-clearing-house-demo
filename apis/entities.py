"""Legal Entity API — GLEIF Global LEI index.

Resolves clearing members / counterparties to their LEI, registration details and
corporate hierarchy. Public, no API key.
"""
from fastapi import APIRouter, HTTPException, Query

from . import clients
from .models import Entity, EntityRelationships, EntitySearch

router = APIRouter(prefix="/entities", tags=["legal-entities"])


# NOTE: the static '/search' route is declared before '/{lei}' so it isn't
# captured as an LEI path parameter.
@router.get("/search", summary="Search legal entities by name", response_model=EntitySearch)
def search(name: str = Query(..., min_length=2, description="(Part of) a legal entity name"),
           limit: int = Query(10, ge=1, le=200)):
    try:
        results = clients.search_entities(name, limit=limit)
    except Exception as e:
        raise HTTPException(502, f"GLEIF upstream error: {e}")
    return EntitySearch(query=name, count=len(results), results=results)


@router.get("/{lei}", summary="Fetch one entity by LEI", response_model=Entity)
def get_one(lei: str):
    try:
        rec = clients.get_entity(lei)
    except Exception as e:
        raise HTTPException(502, f"GLEIF upstream error: {e}")
    if rec is None:
        raise HTTPException(404, f"No LEI record for '{lei}'")
    return rec


@router.get("/{lei}/relationships", summary="Direct + ultimate parent",
            response_model=EntityRelationships)
def relationships(lei: str):
    try:
        return clients.get_relationships(lei)
    except Exception as e:
        raise HTTPException(502, f"GLEIF upstream error: {e}")
