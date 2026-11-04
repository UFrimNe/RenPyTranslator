from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional

@dataclass(frozen=True)
class Candidate:
    kind: str
    text: Optional[str]
    line: int
    column: int = 0
    speaker: Optional[str] = None
    dynamic: bool = False
    expression: Optional[str] = None
    language: Optional[str] = None
    source: str = ""
    label: Optional[str] = None
    translation_id: Optional[str] = None
    translation_id_source: Optional[str] = None
    translation_id_alternate: Optional[str] = None
    say_code: Optional[str] = None
    context_scope: Optional[tuple[str, ...]] = None

    def to_dict(self):
        return asdict(self)
