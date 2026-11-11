"""Install the built wheel into a fresh environment and run the real entry point."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    """Use system packages only for Pillow, with the application installed from wheel."""
    wheel = ROOT / "dist/nju_secondhand-1.0.0-py3-none-any.whl"
    transcripts = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        python = root / ("env/Scripts/python.exe" if os.name == "nt" else "env/bin/python")
        commands = [
            [sys.executable, "-m", "venv", "--system-site-packages", str(root / "env")],
            [str(python), "-m", "pip", "install", "--no-deps", "--force-reinstall", str(wheel)],
            [
                str(python),
                "-I",
                "-c",
                "import secondhand; print(secondhand.__file__); "
                f"assert {str(root / 'env')!r}.lower() in secondhand.__file__.lower()",
            ],
            [str(python), "-I", "-m", "secondhand", "--help"],
            [str(python), "-I", "-m", "secondhand", "--db", str(root / "fresh.db"), "check"],
        ]
        for command in commands:
            result = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, "PYTHONUTF8": "1"},
                check=False,
            )
            transcripts.append(
                {
                    "command": command,
                    "exit_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
            if result.returncode:
                break
    (ROOT / "evidence/wheel-smoke.json").write_text(
        json.dumps(transcripts, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if len(transcripts) != len(commands) or any(row["exit_code"] for row in transcripts):
        raise SystemExit("Wheel smoke test failed; see evidence/wheel-smoke.json")
    print("Wheel installed from artifact; isolated import, CLI and database check passed.")


if __name__ == "__main__":
    main()
