"""Frontend interaction ports and lazy GUI adapter loader."""

from whipit.ui.base import (
    CalibrationAction,
    CalibrationChoice,
    ConflictChoice,
    DiscPromptAction,
    DiscPromptResult,
    ExtractionErrorAction,
    ExtractionErrorChoice,
    LogLevel,
    NullInteractionAdapter,
    UserInteractionPort,
)


def __getattr__(name: str):
    if name == "GuiInteractionAdapter":
        from whipit.ui.gui.adapter import GuiInteractionAdapter
        return GuiInteractionAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CalibrationAction",
    "CalibrationChoice",
    "ConflictChoice",
    "DiscPromptAction",
    "DiscPromptResult",
    "ExtractionErrorAction",
    "ExtractionErrorChoice",
    "GuiInteractionAdapter",
    "LogLevel",
    "NullInteractionAdapter",
    "UserInteractionPort",
]
