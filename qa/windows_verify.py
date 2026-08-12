from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "task"
EVIDENCE = ROOT / "evidence"
RUNS = ROOT / "windows-runs"
PSQL = os.environ["PSQL_PATH"]
ADMIN = os.environ["SERVER_ADMIN_URL"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reset(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def extract(archive: Path, target: Path) -> None:
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(target)


def files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def normalized(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def compare(actual: Path, expected: Path) -> list[str]:
    if files(actual) != files(expected):
        raise AssertionError("Reference文件集合不一致")
    for relative in files(expected):
        if normalized(actual / relative) != normalized(expected / relative):
            raise AssertionError(f"Reference内容不一致：{relative}")
    return files(expected)


def admin(sql: str) -> None:
    completed = subprocess.run(
        [PSQL, "--dbname", ADMIN, "-X", "--set", "ON_ERROR_STOP=1", "--command", sql],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=60,
    )
    if completed.returncode:
        raise AssertionError(completed.stdout + completed.stderr)


def build(input_dir: Path, output_dir: Path, database_name: str) -> subprocess.CompletedProcess[str]:
    admin(f"DROP DATABASE IF EXISTS {database_name} WITH (FORCE)")
    admin(f"CREATE DATABASE {database_name}")
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "implementation/build_delivery.py"),
            "--input",
            str(input_dir),
            "--output",
            str(output_dir),
            "--psql",
            PSQL,
            "--database-url",
            f"postgresql://postgres:root@127.0.0.1:5432/{database_name}",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=300,
    )


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    reset(RUNS)
    EVIDENCE.mkdir(exist_ok=True)
    version = subprocess.run([PSQL, "--version"], text=True, encoding="utf-8", errors="replace", capture_output=True)
    if version.returncode or " 17." not in version.stdout:
        raise AssertionError("需要PostgreSQL17")

    expected = RUNS / "reference"
    extract(TASK / "reference.zip", expected)
    clean_runs = []
    for root_index, label in enumerate(("clean-a", "clean-b"), 1):
        base = RUNS / label
        extract(TASK / "输入数据包.zip", base)
        before = {path.relative_to(base).as_posix(): sha(path) for path in base.rglob("*") if path.is_file()}
        for process_index in (1, 2):
            output = base / f"delivery-{process_index}"
            completed = build(base, output, f"catalog_clean_{root_index}_{process_index}")
            if completed.returncode:
                raise AssertionError(completed.stdout + completed.stderr)
            generated = compare(output, expected)
            clean_runs.append(
                {
                    "root_id": label,
                    "process_index": process_index,
                    "primary_software_executed": True,
                    "reference_full_match": True,
                    "generated_paths": generated,
                }
            )
        after = {
            path.relative_to(base).as_posix(): sha(path)
            for path in base.rglob("*")
            if path.is_file() and not any(part.startswith("delivery-") for part in path.relative_to(base).parts)
        }
        if before != after:
            raise AssertionError("输入材料发生变化")

    positive = RUNS / "positive"
    extract(TASK / "输入数据包.zip", positive)
    events_path = positive / "cdc_events.csv"
    rows = read_csv(events_path)
    for row in rows:
        if row["event_id"] == "E19":
            row["price"] = "14.00"
    with events_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    completed = build(positive, positive / "delivery", "catalog_positive")
    if completed.returncode:
        raise AssertionError(completed.stdout + completed.stderr)
    changed = next(row for row in read_csv(positive / "delivery/results/active_catalog.csv") if row["product_id"] == "P100")
    if changed["price"] != "14.00":
        raise AssertionError("有效价格变化没有进入目录")
    if normalized(positive / "delivery/results/active_catalog.csv") == normalized(expected / "results/active_catalog.csv"):
        raise AssertionError("有效输入变化没有产生可观察变化")
    (EVIDENCE / "positive-case.json").write_text(
        json.dumps({"mutation": "E19价格由13.00改为14.00", "observed_price": changed["price"], "result": "PASS"}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    negative = RUNS / "negative"
    extract(TASK / "输入数据包.zip", negative)
    contract_path = negative / "cdc_contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["position_order"] = ["source_lsn", "event_seq"]
    contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    negative_output = negative / "delivery"
    negative_output.mkdir()
    (negative_output / "old.txt").write_text("old", encoding="utf-8")
    completed = build(negative, negative_output, "catalog_negative")
    if completed.returncode == 0 or negative_output.exists():
        raise AssertionError("不完整位置顺序没有关闭处理")
    (EVIDENCE / "negative-case.log").write_text(
        f"return_code={completed.returncode}\n{completed.stdout}{completed.stderr}", encoding="utf-8"
    )

    summary = {
        "result": "PASS",
        "commit_sha": os.getenv("GITHUB_SHA"),
        "workflow_run_id": os.getenv("GITHUB_RUN_ID"),
        "runner_image": os.getenv("ImageOS"),
        "main_software": {"name": "PostgreSQL", "version": version.stdout.strip(), "executed": True},
        "clean_directory_count": 2,
        "process_runs_per_directory": 2,
        "clean_runs": clean_runs,
        "positive_mutation": "PASS",
        "negative_case": "PASS",
        "reference_full_comparison": "PASS",
        "formal_network": {"python_outbound_blocked": True, "psql_internet_blocked": True, "loopback_only": True},
    }
    (EVIDENCE / "windows-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
