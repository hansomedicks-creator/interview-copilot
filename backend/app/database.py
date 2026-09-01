from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str):
        is_sqlite = url.startswith("sqlite")
        connect_args = {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
        self.engine = create_engine(url, connect_args=connect_args, future=True)
        if is_sqlite:
            @event.listens_for(self.engine, "connect")
            def _configure_sqlite(connection, _connection_record) -> None:
                cursor = connection.cursor()
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()
        self.session_factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False, class_=Session
        )

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        columns = {item["name"] for item in inspect(self.engine).get_columns("transcript_segments")}
        if "provider_speaker_id" not in columns:
            with self.engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE transcript_segments ADD COLUMN provider_speaker_id INTEGER")
                )
        job_columns = {item["name"] for item in inspect(self.engine).get_columns("jobs")}
        if "semantic_profile" not in job_columns:
            with self.engine.begin() as connection:
                connection.execute(text("ALTER TABLE jobs ADD COLUMN semantic_profile JSON"))
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE jobs SET semantic_profile = '{}' "
                    "WHERE semantic_profile IS NULL"
                )
            )
        round_columns = {
            item["name"] for item in inspect(self.engine).get_columns("interview_rounds")
        }
        if "suggestion_history" not in round_columns:
            with self.engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE interview_rounds ADD COLUMN suggestion_history JSON")
                )
        if "interview_mode" not in round_columns:
            with self.engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE interview_rounds ADD COLUMN interview_mode VARCHAR(32)")
                )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE interview_rounds "
                    "SET suggestion_history = '[]' "
                    "WHERE suggestion_history IS NULL"
                )
            )
            connection.execute(
                text(
                    "UPDATE interview_rounds "
                    "SET interview_mode = 'structured' "
                    "WHERE interview_mode IS NULL OR interview_mode = ''"
                )
            )

    def session(self) -> Iterator[Session]:
        with self.session_factory() as session:
            yield session
