"""Pydantic models for drills, artifacts, fragments and compaction generations."""
from __future__ import annotations

import hashlib
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


def fragment_digest(text: str) -> str:
    """Stable content digest for a reusable fragment (sha256, hex)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ArtifactIn(BaseModel):
    """One artifact: an ordered list of short, reusable text fragments."""

    name: str = Field(min_length=1, max_length=200)
    fragments: List[str] = Field(min_length=1)

    @field_validator("fragments")
    @classmethod
    def _nonempty(cls, values: List[str]) -> List[str]:
        cleaned = [f for f in values]
        for i, f in enumerate(cleaned):
            if not isinstance(f, str) or not f.strip():
                raise ValueError(f"fragment #{i + 1} must be non-empty text")
            if len(f) > 100_000:
                raise ValueError(f"fragment #{i + 1} is too long (max 100000 chars)")
        return cleaned


class DrillIn(BaseModel):
    """A drill (exercise) containing 2..8 artifacts."""

    name: str = Field(min_length=1, max_length=200)
    artifacts: List[ArtifactIn] = Field(min_length=2, max_length=8)


class FragmentView(BaseModel):
    digest: str
    size: int
    segment: Optional[str] = None
    offset: Optional[int] = None
    length: Optional[int] = None
    reused: bool = False


class ArtifactSummary(BaseModel):
    name: str
    fragment_count: int
    unique_fragments: int
    reassembled_digest: str
    rebuild_ok: Optional[bool] = None
    fragments: List[FragmentView]


class RecoveryVerdict(BaseModel):
    """Result of verifying that every registered artifact can be rebuilt."""

    complete: bool
    checked_artifacts: int
    checked_fragments: int
    missing_fragments: List[str]
    retransmission: bool
    detail: str


class GenerationView(BaseModel):
    generation: int
    status: Literal["active", "staged", "retired"]
    segment: str
    fragment_count: int
    byte_length: int
    created_at: str


class DrillView(BaseModel):
    id: str
    name: str
    consolidation_id: Optional[str]
    active_generation: Optional[int]
    generations: List[GenerationView]
    artifacts: List[ArtifactSummary]
    recovery: Optional[RecoveryVerdict]
    segments: List[str] = []
    updated_at: str


class CompactRequest(BaseModel):
    consolidation_id: str = Field(min_length=1, max_length=200)
    crash_after: Optional[Literal["segments", "switch", "none"]] = "none"


class RejectReason(BaseModel):
    rejected: bool
    reason: str
    active_generation: Optional[int] = None


class CompactResponse(BaseModel):
    status: Literal["active", "rejected", "simulated_crash"]
    consolidation_id: Optional[str]
    generation: Optional[int]
    drill: Optional[DrillView]
    reject: Optional[RejectReason] = None
    note: Optional[str] = None
