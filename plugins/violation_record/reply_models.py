from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RecordMessage:
    text: str
    images: tuple[Path, ...] = ()


@dataclass(frozen=True)
class StructuredReply:
    records: tuple[RecordMessage, ...]
