from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.orm import Session

from ..models import InterviewRound, Job, TranscriptSegment


class IntelligenceProvider(Protocol):
    """Replaceable boundary for live and post-interview model capabilities."""

    name: str

    def analyze_live(
        self,
        db: Session,
        interview: InterviewRound,
        job: Job,
        latest_segment: TranscriptSegment | None = None,
    ) -> dict[str, Any]: ...

    def draft_scorecard(
        self, db: Session, interview: InterviewRound, job: Job
    ) -> dict[str, Any]: ...

