from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sidera_knowledge.persistence.database import Database
from sidera_knowledge.persistence.repositories import (
    PersistenceConflict,
    SqlAlchemyCommandRepository,
    SqlAlchemyEventRepository,
    SqlAlchemyKnowledgeCandidateRepository,
    SqlAlchemyKnowledgePageRepository,
    SqlAlchemyKnowledgeSourceRepository,
    translate_integrity_error,
)


class SqlAlchemyUnitOfWork:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._session_context = None
        self._session: Session | None = None
        self._completed = False
        self.pages: SqlAlchemyKnowledgePageRepository
        self.sources: SqlAlchemyKnowledgeSourceRepository
        self.candidates: SqlAlchemyKnowledgeCandidateRepository
        self.commands: SqlAlchemyCommandRepository
        self.events: SqlAlchemyEventRepository

    def __enter__(self) -> Self:
        self._session_context = self._database.session()
        self._session = self._session_context.__enter__()
        self.pages = SqlAlchemyKnowledgePageRepository(self._session)
        self.sources = SqlAlchemyKnowledgeSourceRepository(self._session)
        self.candidates = SqlAlchemyKnowledgeCandidateRepository(self._session)
        self.commands = SqlAlchemyCommandRepository(self._session)
        self.events = SqlAlchemyEventRepository(self._session)
        self._completed = False
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None or self._session_context is None:
            return
        if exc_type is not None or not self._completed:
            self._session.rollback()
        self._session_context.__exit__(exc_type, exc_value, traceback)
        self._session = None
        self._session_context = None

    def _require_session(self) -> Session:
        if self._session is None:
            raise RuntimeError("unit of work must be entered before use")
        return self._session

    def commit(self) -> None:
        session = self._require_session()
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            self._completed = True
            raise translate_integrity_error(error) from error
        self._completed = True

    def rollback(self) -> None:
        self._require_session().rollback()
        self._completed = True

    def flush(self) -> None:
        try:
            self._require_session().flush()
        except IntegrityError as error:
            self._require_session().rollback()
            self._completed = True
            raise PersistenceConflict("unique or foreign-key persistence conflict") from error
