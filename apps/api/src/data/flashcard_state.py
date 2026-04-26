"""Spaced-repetition state for individual flashcards (spec §5.3).

The :class:`FlashcardReview` SQLModel lives here for discoverability — every
file that needs it can ``from data.flashcard_state import FlashcardReview``
instead of reaching into ``data.models``. The actual class is defined in
``data.models`` because SQLModel's mapper registry depends on every
``table=True`` class being importable through the same module path that
``data.db`` imports for ``SQLModel.metadata`` registration.

Why a separate module: an Artifact (the ``FLASHCARD_SET`` envelope) is
immutable once persisted, but a card's review state mutates after every
review. Keeping the schedule in its own table preserves the immutability
of the artifact envelope while allowing cheap-and-frequent updates of the
SM-2 fields.
"""

from __future__ import annotations

from data.models import FlashcardReview

__all__ = ["FlashcardReview"]
