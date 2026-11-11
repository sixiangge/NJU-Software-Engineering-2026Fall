"""Create a submission zip from tracked lab files and the verified build artifacts."""

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parent


def main():
    """Include a hash manifest and verify every archived entry against local bytes."""
    tracked = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--", "lab3"],
            cwd=REPOSITORY,
        )
        .decode("utf-8")
        .split("\0")
    )
    files = [REPOSITORY / name for name in tracked if name]
    files.extend((ROOT / "dist").glob("*.whl"))
    files.extend((ROOT / "dist").glob("*.tar.gz"))
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY,
        text=True,
    ).strip()
    destination = ROOT / "实验三_241220153_诸葛皓昀.zip"
    checksums = {}
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            name = "实验三_241220153_诸葛皓昀/" + path.relative_to(ROOT).as_posix()
            content = path.read_bytes()
            archive.writestr(name, content)
            checksums[name] = hashlib.sha256(content).hexdigest()
        archive.writestr(
            "MANIFEST.json",
            json.dumps({"git_commit": commit, "sha256": checksums}, ensure_ascii=False, indent=2),
        )
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        for name, checksum in checksums.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == checksum
    print(
        json.dumps(
            {
                "path": str(destination),
                "files": len(checksums),
                "git_commit": commit,
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
