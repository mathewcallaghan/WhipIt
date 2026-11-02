"""WhipIt domain services for drive safety, extraction, staging, transcoding, publication, recovery, storage, and validation."""

__all__ = [
    "DriveService",
    "ExtractionService",
    "PublicationService",
    "RecoveryService",
    "StageMetadataService",
    "StorageService",
    "TranscodeService",
    "ValidationService",
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
    if name == "RecoveryService":
        from whipit.services.recovery_service import RecoveryService
        return RecoveryService
    if name == "StageMetadataService":
        from whipit.services.stage_metadata_service import StageMetadataService
        return StageMetadataService
    if name == "StorageService":
        from whipit.services.storage_service import StorageService
        return StorageService
    if name == "TranscodeService":
        from whipit.services.transcode_service import TranscodeService
        return TranscodeService
    if name == "ValidationService":
        from whipit.services.validation_service import ValidationService
        return ValidationService
