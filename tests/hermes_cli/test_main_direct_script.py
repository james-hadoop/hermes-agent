import os
import subprocess
import sys
from pathlib import Path


def test_main_py_direct_script_works_outside_repo_cwd(tmp_path):
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["HERMES_HOME"] = str(tmp_path / "hermes-home")

    result = subprocess.run(
        [sys.executable, str(repo_root / "hermes_cli" / "main.py"), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "Hermes Agent - AI assistant" in result.stdout
