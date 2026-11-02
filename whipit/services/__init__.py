"""WhipIt domain services for drive safety, extraction, staging, transcoding, and publication."""

__all__ = [
    "DriveService",
    "ExtractionService",
    "PublicationService",
    "StageMetadataService",
    "TranscodeService",
]


def __getattr__(name: str):
    if name == "DriveService":
        from whipit.services.drive_service import DriveService
        return DriveService
    if name == "ExtractionService":
        from whipit.services.extraction_service import ExtractionService
        return ExtractionService
    if name == "PublicationService":
        from whipit.services.publication_service import PublicationService
        return PublicationService
    if name == "StageMetadataService":
        from whipit.services.stage_metadata_service import StageMetadataService
        return StageMetadataService
    if name == "TranscodeService":
        from whipit.services.transcode_service import TranscodeService
        return TranscodeService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
