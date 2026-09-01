from __future__ import annotations

from typing import Any, Protocol


class RecruitingSourceAdapter(Protocol):
    async def import_context(self, external_id: str) -> dict[str, Any]: ...


class InterviewResultSink(Protocol):
    async def publish_scorecard(self, payload: dict[str, Any]) -> str: ...

    async def create_interviewer_tasks(self, payload: dict[str, Any]) -> list[str]: ...

    async def propose_stage_change(self, payload: dict[str, Any]) -> str:
        """Create a human-confirmed proposal; never mutate stage automatically."""
        ...


class MeetingArtifactAdapter(Protocol):
    async def fetch_transcript(self, meeting_id: str) -> list[dict[str, Any]]: ...

    async def fetch_recording_link(self, meeting_id: str) -> str | None: ...

