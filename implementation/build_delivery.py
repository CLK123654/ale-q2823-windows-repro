from __future__ import annotations

import argparse
import atexit
import csv
import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SQL_ROOT = ROOT / "sql"


def run(command: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        input=stdin,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=300,
    )
    if completed.returncode:
        raise RuntimeError(completed.stdout + completed.stderr)
    return completed


def psql(binary: str, database_url: str) -> list[str]:
    return [binary, "--dbname", database_url, "-X", "--set", "ON_ERROR_STOP=1"]


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def literal(value: str | None) -> str:
    if value in (None, ""):
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def export(binary: str, database_url: str, query: str, path: Path) -> None:
    command = psql(binary, database_url) + [
        "--quiet",
        "--command",
        f"COPY ({query}) TO STDOUT WITH(FORMAT CSV,HEADER TRUE)",
    ]
    path.write_text(run(command).stdout.replace("\r\n", "\n"), encoding="utf-8", newline="")


def projection(binary: str, database_url: str) -> str:
    queries = [
        "SELECT row_to_json(x)::text FROM (SELECT * FROM core.catalog_state ORDER BY product_id)x",
        "SELECT row_to_json(x)::text FROM (SELECT * FROM ops.apply_decision ORDER BY batch_id,event_id)x",
        "SELECT row_to_json(x)::text FROM (SELECT * FROM ops.batch_receipt ORDER BY batch_id)x",
    ]
    parts = []
    for query in queries:
        parts.append(
            run(psql(binary, database_url) + ["--tuples-only", "--no-align", "--command", query]).stdout
        )
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--psql", required=True)
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args()
    input_root = Path(args.input).resolve()
    output_root = Path(args.output).resolve()
    stage = output_root.with_name(output_root.name + ".stage")
    for path in (output_root, stage):
        if path.exists():
            shutil.rmtree(path)

    finished = {"value": False}

    def clean_failed_stage() -> None:
        if not finished["value"]:
            for path in (output_root, stage):
                if path.exists():
                    shutil.rmtree(path)

    atexit.register(clean_failed_stage)

    contract = json.loads((input_root / "cdc_contract.json").read_text(encoding="utf-8"))
    actual_files = sorted(path.relative_to(input_root).as_posix() for path in input_root.rglob("*") if path.is_file())
    if actual_files != sorted(contract["exact_input_files"]):
        raise ValueError("目录材料集合发生变化")
    base_headers, base_rows = read_csv(input_root / "base_catalog.csv")
    event_headers, event_rows = read_csv(input_root / "cdc_events.csv")
    if base_headers != contract["base_headers"] or event_headers != contract["event_headers"]:
        raise ValueError("目录材料表头与入库合同不一致")
    if not base_rows or len({row["product_id"] for row in base_rows}) != len(base_rows):
        raise ValueError("基线商品主键缺失或重复")
    if not event_rows or any(row["batch_id"] not in contract["batch_order"] for row in event_rows):
        raise ValueError("批次事件为空或批次不在合同内")
    if contract.get("position_order") != ["source_lsn", "event_seq", "event_id"]:
        raise ValueError("目录位置顺序不完整")

    setup = "DROP SCHEMA IF EXISTS core CASCADE; DROP SCHEMA IF EXISTS ops CASCADE;\n"
    setup += (SQL_ROOT / "00_schema.sql").read_text(encoding="utf-8")
    run(psql(args.psql, args.database_url), stdin=setup)

    base_values = []
    for row in base_rows:
        if not row["product_id"] or not row["name"] or not row["price"] or not row["base_lsn"].isdigit():
            raise ValueError("基线目录字段不完整")
        base_values.append(
            "(" + ",".join(
                [
                    literal(row["product_id"]),
                    literal(row["name"]),
                    literal(row["price"]) + "::numeric",
                    "false",
                    row["base_lsn"],
                    "0",
                    literal("BASE-" + row["product_id"]),
                ]
            ) + ")"
        )
    run(
        psql(args.psql, args.database_url)
        + ["--command", "INSERT INTO core.catalog_state VALUES" + ",".join(base_values)],
    )

    event_values = []
    for row in event_rows:
        deletion = row["op"] == "D"
        if row["op"] not in {"I", "U", "D"} or not row["source_lsn"].isdigit() or not row["event_seq"].isdigit():
            raise ValueError("变更事件字段不完整")
        if deletion != (row["name"] == "" and row["price"] == ""):
            raise ValueError("事件操作与业务值不一致")
        event_values.append(
            "(" + ",".join(
                [
                    literal(row["event_id"]),
                    literal(row["batch_id"]),
                    literal(row["product_id"]),
                    literal(row["op"]),
                    row["source_lsn"],
                    row["event_seq"],
                    literal(row["name"]),
                    "NULL" if row["price"] == "" else literal(row["price"]) + "::numeric",
                ]
            ) + ")"
        )
    load_event_sql = (
        "INSERT INTO ops.cdc_raw VALUES"
        + ",".join(event_values)
        + " ON CONFLICT(event_id) DO NOTHING"
    )
    run(psql(args.psql, args.database_url) + ["--command", load_event_sql])
    run(psql(args.psql, args.database_url), stdin=(SQL_ROOT / "20_apply_batch.sql").read_text(encoding="utf-8"))
    run(psql(args.psql, args.database_url), stdin=(SQL_ROOT / "30_replay_batches.sql").read_text(encoding="utf-8"))

    before_repeat = projection(args.psql, args.database_url)
    last_batch = contract["batch_order"][-1]
    run(psql(args.psql, args.database_url) + ["--command", f"SELECT ops.apply_batch({literal(last_batch)})"])
    repeat_zero_drift = before_repeat == projection(args.psql, args.database_url)
    if not repeat_zero_drift:
        raise ValueError("重复批次改变了目录状态")

    (stage / "sql").mkdir(parents=True)
    (stage / "results").mkdir()
    for name in ("00_schema.sql", "20_apply_batch.sql", "30_replay_batches.sql"):
        shutil.copy2(SQL_ROOT / name, stage / "sql" / name)

    export(args.psql, args.database_url, "SELECT product_id,name,price::text price,is_deleted::text,last_lsn::text,last_seq::text,last_event_id FROM core.catalog_state ORDER BY product_id", stage / "results/catalog_state.csv")
    export(args.psql, args.database_url, "SELECT product_id,name,price::text price,is_deleted::text,last_lsn::text,last_seq::text,last_event_id FROM core.catalog_state WHERE NOT is_deleted ORDER BY product_id", stage / "results/active_catalog.csv")
    export(args.psql, args.database_url, "SELECT event_id,batch_id,product_id,decision,winner_event_id,decided_against_lsn::text,decided_against_seq::text,decided_against_event_id FROM ops.apply_decision ORDER BY batch_id,event_id", stage / "results/decision_ledger.csv")
    export(args.psql, args.database_url, "SELECT batch_id,raw_events::text,applied::text,stale::text,superseded::text,state_rows::text,active_rows::text,deleted_rows::text FROM ops.batch_receipt ORDER BY batch_id", stage / "results/batch_receipts.csv")

    duplicate_count = len(event_rows) - len({row["event_id"] for row in event_rows})
    duplicates = sorted({row["event_id"] for row in event_rows if sum(item["event_id"] == row["event_id"] for item in event_rows) > 1})
    raw_receipt = "source_rows,inserted_unique_events,duplicate_event_ids,duplicate_identity\n"
    raw_receipt += f"{len(event_rows)},{len(event_rows) - duplicate_count},{duplicate_count},{';'.join(duplicates)}\n"
    (stage / "results/raw_load_receipt.csv").write_text(raw_receipt, encoding="utf-8", newline="")

    decision_rows = run(
        psql(args.psql, args.database_url)
        + ["--tuples-only", "--no-align", "--field-separator", "|", "--command", "SELECT decision,count(*) FROM ops.apply_decision GROUP BY decision ORDER BY decision"],
    ).stdout.splitlines()
    decisions = {line.split("|", 1)[0]: int(line.split("|", 1)[1]) for line in decision_rows if line}
    state_counts = run(
        psql(args.psql, args.database_url)
        + ["--tuples-only", "--no-align", "--field-separator", "|", "--command", "SELECT count(*),count(*) FILTER(WHERE NOT is_deleted),count(*) FILTER(WHERE is_deleted) FROM core.catalog_state"],
    ).stdout.strip().split("|")
    summary = {
        "contract_id": contract["contract_id"],
        "engine": "PostgreSQL 17",
        "base_rows": len(base_rows),
        "source_rows": len(event_rows),
        "unique_events": len(event_rows) - duplicate_count,
        "duplicate_event_ids": duplicate_count,
        "duplicate_identity": ";".join(duplicates),
        "decisions": {name: decisions.get(name, 0) for name in ("APPLY", "STALE", "SUPERSEDED_IN_BATCH")},
        "state_rows": int(state_counts[0]),
        "active_rows": int(state_counts[1]),
        "deleted_rows": int(state_counts[2]),
        "repeat_B3_zero_drift": True,
    }
    (stage / "results/cdc_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(ROOT / "delivery_README.md", stage / "README.md")
    stage.rename(output_root)
    finished["value"] = True


if __name__ == "__main__":
    main()
