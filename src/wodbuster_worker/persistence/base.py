"""SQLAlchemy declarative base for the persistence layer.

Kept in its own module so tests and Alembic env can import the metadata
without pulling in every model. Models attach to ``Base`` in
``wodbuster_worker.persistence.models``.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base shared by every model.

    Ships no naming convention override. SQLite generates readable
    default names and Alembic autogenerate produces stable, review-able
    diffs against them.
    """
