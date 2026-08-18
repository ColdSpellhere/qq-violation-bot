import random
from pathlib import Path


SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _bounded(value: float) -> float:
    return min(1.0, max(0.0, value))


def _available_stickers(root: Path) -> list[Path]:
    try:
        return sorted(
            (
                path
                for path in root.iterdir()
                if path.is_file()
                and not path.is_symlink()
                and path.suffix.casefold() in SUPPORTED_SUFFIXES
            ),
            key=lambda path: path.name,
        )
    except OSError:
        return []


def choose_sticker(
    root: Path,
    *,
    special_filename: str,
    attachment_probability: float,
    attachment_sample: float | None = None,
    weight_sample: float | None = None,
) -> Path | None:
    probability = _bounded(attachment_probability)
    attachment_roll = random.random() if attachment_sample is None else attachment_sample
    if probability <= 0.0 or attachment_roll >= probability:
        return None

    stickers = _available_stickers(Path(root))
    if not stickers:
        return None

    special_name = Path(special_filename).name
    special = next((path for path in stickers if path.name == special_name), None)
    normal = [path for path in stickers if path != special]
    if not normal:
        return special

    roll = random.random() if weight_sample is None else min(0.999999999999, max(0.0, weight_sample))
    if special is not None and roll < 0.10:
        return special

    normal_roll = (roll - 0.10) / 0.90 if special is not None else roll
    index = min(int(normal_roll * len(normal)), len(normal) - 1)
    return normal[index]
