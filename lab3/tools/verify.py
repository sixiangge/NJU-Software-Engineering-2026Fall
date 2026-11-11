"""Reproducible build, lint, tests and source accounting with actual exit codes."""

import ast
import io
import json
import platform
import sqlite3
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"


def source_counts():
    """Exclude tests, tools, empty lines, comments and docstrings from strict counts."""
    rows = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        excluded = set()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    excluded.update(range(body[0].lineno, body[0].end_lineno + 1))
        comments = {
            token.start[0]
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT
        }
        lines = source.splitlines()
        strict = sum(
            bool(line.strip())
            and index not in excluded
            and not (index in comments and line.lstrip().startswith("#"))
            for index, line in enumerate(lines, 1)
        )
        rows.append({"file": str(path.relative_to(ROOT)), "physical": len(lines), "code": strict})
    result = {
        "files": rows,
        "physical": sum(row["physical"] for row in rows),
        "code_without_blank_comment_docstring": sum(row["code"] for row in rows),
    }
    (EVIDENCE / "source-counts.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if result["code_without_blank_comment_docstring"] <= 1500:
        raise RuntimeError("Application code must exceed 1500 lines")
    return result


def run(name, arguments):
    """Capture UTF-8 directly, bypassing PowerShell stderr reformatting."""
    process = subprocess.run(
        arguments,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        env={**__import__("os").environ, "PYTHONUTF8": "1"},
        check=False,
    )
    (EVIDENCE / f"{name}.txt").write_text(
        f"COMMAND: {arguments}\nEXIT CODE: {process.returncode}\n\n{process.stdout}",
        encoding="utf-8",
    )
    print(f"{name}: exit {process.returncode}", flush=True)
    if process.returncode:
        print(process.stdout)
        raise SystemExit(process.returncode)


def main():
    """Stop on failure; do not overwrite failed results with a claimed success."""
    EVIDENCE.mkdir(exist_ok=True)
    counts = source_counts()
    environment = {
        "python": sys.version,
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "application_lines": counts,
    }
    (EVIDENCE / "environment.json").write_text(
        json.dumps(environment, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    run("lint", [sys.executable, "-m", "ruff", "check", "."])
    run("format", [sys.executable, "-m", "ruff", "format", "--check", "."])
    run(
        "tests",
        [
            sys.executable,
            "-W",
            "error::ResourceWarning",
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-v",
        ],
    )
    run("compile", [sys.executable, "-m", "compileall", "-q", "src"])
    run("build", [sys.executable, "-m", "build", "--no-isolation"])
    run("demo", [sys.executable, "tools/demo.py"])
    run("cli-help", [sys.executable, "-m", "secondhand", "--help"])
    print(f"Application code lines: {counts['code_without_blank_comment_docstring']}")


if __name__ == "__main__":
    main()
