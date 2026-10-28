import os
import re
import stat
import subprocess
import sys
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_requirements_lock_syntax_and_hashes():
    """Validates that requirements.lock is well-formed and every package is pinned with sha256 hashes."""
    lock_file = REPO_ROOT / "requirements.lock"
    assert lock_file.exists(), "requirements.lock must exist"

    content = lock_file.read_text(encoding="utf-8")
    blocks = [b.strip() for b in re.split(r"\n\s*\n", content) if b.strip()]

    packages_found = {}
    for block in blocks:
        lines = [ln.strip().rstrip("\\").strip() for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        if not lines:
            continue
        pkg_spec = lines[0]
        assert "==" in pkg_spec, f"Package must be pinned with exact version (==): {pkg_spec}"
        pkg_name, pkg_ver = pkg_spec.split("==", 1)
        hashes = [ln for ln in lines[1:] if ln.startswith("--hash=sha256:")]
        assert len(hashes) >= 1, f"Package {pkg_name} must have at least one sha256 hash"
        packages_found[pkg_name.lower().replace("_", "-")] = pkg_ver

    # Invariant: Critical dependencies and transitive requirements must all be present
    critical_pkgs = [
        "pydantic",
        "pydantic-core",
        "annotated-types",
        "typing-inspection",
        "typing-extensions",
        "pyqt6",
        "pyqt6-qt6",
        "pyqt6-sip",
    ]
    for cp in critical_pkgs:
        assert cp in packages_found, f"Critical dependency '{cp}' is missing from requirements.lock"


def test_install_script_cli_options():
    """Ensures install.sh parses options, displays usage, and rejects unknown flags with exit code 1."""
    install_sh = REPO_ROOT / "install.sh"
    assert install_sh.exists()

    res_help = subprocess.run(["bash", str(install_sh), "--help"], capture_output=True, text=True)
    assert res_help.returncode == 0
    assert "Usage:" in res_help.stdout
    assert "--editable" in res_help.stdout

    res_short_help = subprocess.run(["bash", str(install_sh), "-h"], capture_output=True, text=True)
    assert res_short_help.returncode == 0
    assert "Usage:" in res_short_help.stdout

    res_unknown = subprocess.run(["bash", str(install_sh), "--bogus-flag"], capture_output=True, text=True)
    assert res_unknown.returncode == 1
    assert "Unknown option: --bogus-flag" in res_unknown.stdout or "Unknown option: --bogus-flag" in res_unknown.stderr


def test_install_script_rejects_outdated_python(tmp_path):
    """Ensures install.sh halts immediately if python3 is older than 3.9."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    fake_py = fake_bin / "python3"
    fake_py.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == *\"sys.version_info\"* ]]; then\n"
        "    if [[ \"$*\" == *\"major\"* ]]; then\n"
        "        echo '3.8'\n"
        "    fi\n"
        "    exit 1\n"
        "fi\n"
        "exec python3 \"$@\"\n"
    )
    fake_py.chmod(0o755)

    install_sh = REPO_ROOT / "install.sh"
    custom_env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
    }

    res = subprocess.run(["bash", str(install_sh)], capture_output=True, text=True, env=custom_env)
    assert res.returncode == 1
    assert "Python 3.9 or newer is required" in res.stdout or "Python 3.9 or newer is required" in res.stderr


def test_isolated_sandbox_install_and_uninstall(tmp_path):
    """
    Executes install.sh and uninstall.sh in a completely isolated filesystem sandbox,
    verifying directories, permissions (0700/0600), symlinks, and full uninstall cleanup.
    """
    fake_home = tmp_path / "home"
    fake_data = tmp_path / "data"
    fake_config = tmp_path / "config"
    fake_cache = tmp_path / "cache"
    fake_state = tmp_path / "state"

    fake_home.mkdir()
    fake_data.mkdir()
    fake_config.mkdir()
    fake_cache.mkdir()
    fake_state.mkdir()

    # Create a lightweight stub for pip to keep this test instant and offline
    fake_tools = tmp_path / "tools"
    fake_tools.mkdir()
    stub_launcher = fake_tools / "stub_launcher.py"
    stub_launcher.write_text("def install_launcher(): return True, 'ok'\n")

    sandbox_env = {
        **os.environ,
        "HOME": str(fake_home),
        "XDG_DATA_HOME": str(fake_data),
        "XDG_CONFIG_HOME": str(fake_config),
        "XDG_CACHE_HOME": str(fake_cache),
        "XDG_STATE_HOME": str(fake_state),
    }

    install_sh = REPO_ROOT / "install.sh"
    uninstall_sh = REPO_ROOT / "uninstall.sh"

    # Pre-create virtual environment with a fast dummy pip to test the shell script mechanics
    venv_dir = fake_data / "whipit" / "venv"
    venv_bin = venv_dir / "bin"
    venv_bin.mkdir(parents=True)
    venv_py = venv_bin / "python"
    venv_py.write_text(
        f"#!/usr/bin/env bash\n"
        f"if [[ \"$*\" == *\"-m pip\"* ]]; then\n"
        f"    exit 0\n"
        f"fi\n"
        f"if [[ \"$*\" == *\"import whipit\"* ]]; then\n"
        f"    echo '2.5.7'\n"
        f"    exit 0\n"
        f"fi\n"
        f"if [[ \"$*\" == *\"install_launcher\"* ]]; then\n"
        f"    exit 0\n"
        f"fi\n"
        f"if [[ \"$*\" == *\"import sys\"* ]]; then\n"
        f"    exit 0\n"
        f"fi\n"
        f"exec \"{sys.executable}\" \"$@\"\n"
    )
    venv_py.chmod(0o755)
    (venv_bin / "python3").symlink_to(venv_py)

    # Stub whipit executable inside venv
    whipit_exec = venv_bin / "whipit"
    whipit_exec.write_text("#!/usr/bin/env bash\necho 'whipit 99.9.9'\n")
    whipit_exec.chmod(0o755)

    # 1. Run install.sh in sandbox
    res_install = subprocess.run(["bash", str(install_sh)], capture_output=True, text=True, env=sandbox_env)
    assert res_install.returncode == 0, f"install.sh failed:\nSTDOUT:\n{res_install.stdout}\nSTDERR:\n{res_install.stderr}"

    # 2. Assert filesystem invariants
    bin_link = fake_home / ".local" / "bin" / "whipit"
    assert bin_link.is_symlink()
    assert bin_link.resolve() == whipit_exec.resolve()

    cfg_dir = fake_config / "whipit"
    assert cfg_dir.exists()
    assert stat.S_IMODE(cfg_dir.stat().st_mode) == 0o700

    cfg_file = cfg_dir / "config"
    if cfg_file.exists():
        assert stat.S_IMODE(cfg_file.stat().st_mode) == 0o600
        assert f"source_dir={REPO_ROOT}" in cfg_file.read_text()

    # 3. Run uninstall.sh with --purge -y in sandbox
    res_uninstall = subprocess.run(
        ["bash", str(uninstall_sh), "--purge", "-y"],
        capture_output=True,
        text=True,
        env=sandbox_env,
    )
    assert res_uninstall.returncode == 0, f"uninstall.sh failed:\nSTDOUT:\n{res_uninstall.stdout}\nSTDERR:\n{res_uninstall.stderr}"

    # 4. Assert clean removal
    assert not bin_link.exists()
    assert not (fake_data / "whipit").exists()
    assert not cfg_dir.exists()
