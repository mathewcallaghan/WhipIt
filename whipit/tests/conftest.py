import os
import shutil
import tempfile
from pathlib import Path
import pytest
from whipit.config import get_cache_dir
from whipit.models import AlbumMetadata, DeviceInfo, JobManifest, PathLayout, TransactionJournal


@pytest.fixture
def temp_env():
    root = Path(tempfile.mkdtemp(prefix="whipit_test_"))
    output = root / "WhipIt"
    output.mkdir()
    cache = root / "cache"
    cache.mkdir()
    state = root / "state"
    state.mkdir()
    config = root / "config"
    config.mkdir()

    old_env = os.environ.copy()
    os.environ["WHIPIT_OUTPUT_DIR"] = str(output)
    os.environ["XDG_CACHE_HOME"] = str(cache)
    os.environ["XDG_STATE_HOME"] = str(state)
    os.environ["XDG_CONFIG_HOME"] = str(config)

    yield {"root": root, "output": output, "cache": cache, "state": state, "config": config}

    os.environ.clear()
    os.environ.update(old_env)
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def sample_manifest(temp_env):
    output = temp_env["output"]
    cache = get_cache_dir()
    job_id = "test-job-uuid-1234"
    return JobManifest(
        job_id=job_id,
        device=DeviceInfo(path="/dev/sr0", hardware_id="HL-DT-ST_BD-RE_WH16NS40", read_offset=6),
        metadata=AlbumMetadata(artist="Pink Floyd", album="The Wall", album_stem="Pink Floyd - The Wall"),
        paths=PathLayout(
            output_root=output,
            whipper_stage=cache / f"stage_{job_id}",
            candidate_stage=output / f".whipit-stage.{job_id}",
            final_flac_dir=output / "Pink Floyd - The Wall" / "Pink Floyd - The Wall (FLAC)",
            final_opus_dir=output / "Pink Floyd - The Wall" / "Pink Floyd - The Wall (Opus)",
        ),
        transaction=TransactionJournal(),
    )
