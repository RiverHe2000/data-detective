"""Verify this checkout with the current interpreter; write a reproducible evidence bundle.

Usage: .venv/Scripts/python.exe scripts/verify.py
This does not install packages, create an environment, or run GPU/model inference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import site
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("data-detective", "pandas", "streamlit", "plotly", "pytest", "ruff", "openpyxl", "requests")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root: Path) -> dict[str, str]:
    paths = [root / "app.py", root / "pyproject.toml"]
    paths.extend(root.glob("requirements*.lock"))
    for directory in ("src/data_detective", "tests", "scripts"):
        paths.extend((root / directory).rglob("*.py"))
    paths.extend((root / "data/cases").rglob("*.json"))
    paths.extend((root / "data/cases").rglob("*.csv"))
    return {
        path.relative_to(root).as_posix(): file_hash(path) for path in sorted(set(paths)) if path.is_file()
    }


def changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))


def environment() -> dict:
    packages = {}
    for name in PACKAGES:
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    config = Path(sys.prefix) / "pyvenv.cfg"
    values = {}
    if config.is_file():
        values = dict(
            line.split("=", 1) for line in config.read_text(encoding="utf-8").splitlines() if "=" in line
        )
        values = {key.strip(): value.strip() for key, value in values.items()}
    inherited = values.get("include-system-site-packages")
    return {
        "python_executable": sys.executable,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "is_venv": sys.prefix != sys.base_prefix,
        "include_system_site_packages": None if inherited is None else inherited.lower() == "true",
        "user_site_enabled": site.ENABLE_USER_SITE,
        "packages": packages,
        "installation_mode": "Inspect existing interpreter; no packages installed or environment created by this run",
        "clean_install_verified": False,
    }


def run_check(name: str, arguments: list[str], output: Path, *, timeout: float = 300) -> dict:
    env = os.environ.copy()
    env.update(
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        CUDA_VISIBLE_DEVICES="",
    )
    command = [sys.executable, *arguments]
    started = time.perf_counter()
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        stdout, stderr, code = result.stdout, result.stderr, result.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        stdout, stderr, code = exc.stdout or b"", exc.stderr or b"", 124
        timed_out = True
    except OSError as exc:
        stdout, stderr, code = b"", str(exc).encode("utf-8"), 125
        timed_out = False
    stdout_path, stderr_path = output / f"{name}.stdout.txt", output / f"{name}.stderr.txt"
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    return dict(
        name=name,
        command=command,
        exit_code=code,
        passed=code == 0,
        timed_out=timed_out,
        elapsed_seconds=round(time.perf_counter() - started, 3),
        stdout_file=stdout_path.name,
        stderr_file=stderr_path.name,
        stdout_sha256=file_hash(stdout_path),
        stderr_sha256=file_hash(stderr_path),
        stdout_tail=stdout.decode("utf-8", errors="replace")[-12_000:],
        stderr_tail=stderr.decode("utf-8", errors="replace")[-6_000:],
    )


def case_gate(report: dict) -> dict:
    """A script exiting zero is insufficient: check the frozen benchmark's outcomes."""
    failures = []
    try:
        summary = report["summary"]
        if summary["dev"]["cases"] != 6 or summary["test"]["cases"] != 6 or summary["all"]["cases"] != 12:
            failures.append("Expected six development and six test cases")
        expected_kinds = {
            "copied_records",
            "missing_value",
            "numeric_corruption",
            "date_corruption",
            "price_spike",
        }
        for split in ("dev", "test", "all"):
            item = summary[split]
            if set(item["injections"]) != expected_kinds:
                failures.append(f"{split}: injection families are incomplete")
            for kind, score in item["injections"].items():
                if score["events"] <= 0 or score["detected"] != score["events"]:
                    failures.append(f"{split}/{kind}: missed known injection events")
                if score["target_rows"] <= 0 or score["detected_rows"] != score["target_rows"]:
                    failures.append(f"{split}/{kind}: missed known target rows")
            if not item["controls"]:
                failures.append(f"{split}: controls missing")
            for kind, score in item["controls"].items():
                if score["rows"] <= 0 or score["error_rows"] or score["unexpected_review_rows"]:
                    failures.append(f"{split}/{kind}: control regression")
            if item["oracle_restorations"] != item["cases"]:
                failures.append(f"{split}: an oracle repair did not restore baseline rows")
            if (
                item["independent_metric_checks"] != item["independent_metric_check_total"]
                or item["independent_metric_check_total"] != 3 * item["cases"]
            ):
                failures.append(f"{split}: independent financial checks failed or were incomplete")
    except (KeyError, TypeError, AttributeError) as exc:
        failures.append(f"Malformed evaluation report: {type(exc).__name__}")
    return {
        "passed": not failures,
        "failures": failures,
        "scope": "Regression gate over known edits and stipulated controls, not production accuracy",
    }


def junit_counts(path: Path) -> dict | None:
    if not path.is_file():
        return None
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    return {
        key: sum(int(suite.get(key, "0")) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }


def tests_passed(counts: dict | None) -> bool:
    return (
        counts is not None
        and counts["tests"] > counts["skipped"]
        and not counts["failures"]
        and not counts["errors"]
    )


def save_report(output: Path, report: dict) -> None:
    (output / "verification.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    lines = [
        "# Core verification",
        "",
        f"Status: **{report['status']}**.",
        "",
        "This run inspected the current Python environment. It did not perform a clean install, "
        "create a virtual environment, download data, or run real model/GPU inference.",
        "",
        f"Python: `{report['environment']['python']}` · `{report['environment']['platform']}`.",
        f"System site packages inherited: `{report['environment']['include_system_site_packages']}`.",
        "",
        "| Check | Result | Seconds |",
        "|---|---|---:|",
    ]
    for check in report["checks"]:
        lines.append(
            f"| {check['name']} | {'PASS' if check['passed'] else 'FAIL'} | {check['elapsed_seconds']:.3f} |"
        )
    if report.get("case_gate"):
        lines.append(f"| frozen_case_gate | {'PASS' if report['case_gate']['passed'] else 'FAIL'} | — |")
    if report.get("pytest_counts"):
        lines += ["", "Pytest counts: `" + json.dumps(report["pytest_counts"]) + "`."]
    if report.get("changed_during_run"):
        lines += [
            "",
            "Source changed while checking; the run is invalidated. Rerun after edits finish.",
            "",
            *[f"- `{name}`" for name in report["changed_during_run"]],
        ]
    if report.get("case_gate", {}).get("failures"):
        lines += ["", *[f"- {message}" for message in report["case_gate"]["failures"]]]
    if report.get("error"):
        lines += ["", report["error"]]
    lines += [
        "",
        "The JSON receipt records source/case hashes, commands, environment, output hashes and test counts. "
        "Full per-command logs, JUnit XML and case evaluation files are retained beside it.",
        "",
    ]
    (output / "verification.md").write_text("\n".join(lines), encoding="utf-8")


def verify(output: Path) -> int:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = dict(
        schema_version=1,
        status="running",
        started_at=now(),
        checkout=str(ROOT),
        environment=environment(),
        files_before=snapshot(ROOT),
        checks=[],
    )
    save_report(output, report)
    # -I verifies the installed package rather than obtaining it accidentally through CWD/PYTHONPATH.
    installed_probe = (
        "import importlib,importlib.metadata,json,pathlib,sys; "
        "names=['data_detective.engine','data_detective.store','data_detective.exports','pandas','streamlit','plotly']; "
        "modules={name:str(pathlib.Path(importlib.import_module(name).__file__).resolve()) for name in names}; "
        "expected=pathlib.Path(sys.argv[1]).resolve()/'src'/'data_detective'; "
        "assert pathlib.Path(modules['data_detective.engine']).parent==expected,'Installed package is not this checkout'; "
        "print(json.dumps({'project_version':importlib.metadata.version('data-detective'),'modules':modules},indent=2))"
    )
    commands = [
        ("installed_core", ["-I", "-c", installed_probe, str(ROOT)]),
        ("dependencies", ["-m", "pip", "check"]),
        ("lint", ["-m", "ruff", "check", "src", "tests", "scripts"]),
        (
            "tests",
            [
                "-m",
                "pytest",
                "-q",
                "-m",
                "not model and not performance",
                f"--junitxml={output / 'pytest.xml'}",
            ],
        ),
        ("case_evaluation", [str(ROOT / "scripts/evaluate.py"), "--out", str(output / "cases")]),
    ]
    try:
        for name, command in commands:
            print(f"Checking {name}...", flush=True)
            check = run_check(name, command, output)
            report["checks"].append(check)
            save_report(output, report)
            print(
                f"{name}: {'PASS' if check['passed'] else 'FAIL'} ({check['elapsed_seconds']:.3f}s)",
                flush=True,
            )
        evaluation_check = next(check for check in report["checks"] if check["name"] == "case_evaluation")
        evaluation_path = output / "cases/evaluation.json"
        if evaluation_check["passed"] and evaluation_path.is_file():
            report["case_gate"] = case_gate(json.loads(evaluation_path.read_text(encoding="utf-8")))
            report["case_evaluation_sha256"] = file_hash(evaluation_path)
        else:
            report["case_gate"] = {
                "passed": False,
                "failures": ["Case evaluation did not complete successfully"],
            }
        report["pytest_counts"] = junit_counts(output / "pytest.xml")
        counts = report["pytest_counts"]
        nonempty_tests = tests_passed(counts)
        report["status"] = (
            "pass"
            if all(check["passed"] for check in report["checks"])
            and report["case_gate"]["passed"]
            and nonempty_tests
            else "fail"
        )
    except KeyboardInterrupt:
        report["status"] = "interrupted"
    except (OSError, ValueError, ET.ParseError) as exc:
        report["status"] = "fail"
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        report["files_after"] = snapshot(ROOT)
        report["changed_during_run"] = changed_files(report["files_before"], report["files_after"])
        if report["changed_during_run"] and report["status"] != "interrupted":
            report["status"] = "invalidated"
        report["finished_at"] = now()
        save_report(output, report)
    print(f"Verification {report['status']}: {output / 'verification.json'}", flush=True)
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=ROOT / "reports/verification", help="Evidence bundle directory"
    )
    args = parser.parse_args()
    raise SystemExit(verify(args.out))
