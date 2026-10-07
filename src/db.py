"""Database access, schema application, and contract-to-dimension sync.

Run `python src/db.py` to apply every sql/*.sql file in order and sync dim_rule.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import psycopg

from contract import CONTRACT, RULES_BY_CODE

BASE_DIR = Path(__file__).resolve().parent.parent
SQL_DIR = BASE_DIR / "sql"

DEFAULT_DSN = (
    "host=127.0.0.1 port=5432 dbname=finance_capstone user=postgres password=admin"
)


def connection_string() -> str:
    return os.getenv("DATABASE_URL", DEFAULT_DSN)


def connect(**kwargs) -> psycopg.Connection:
    return psycopg.connect(connection_string(), **kwargs)


def _split_statements(sql: str) -> list[str]:
    """Split a script into statements on top-level semicolons.

    A semicolon only terminates a statement when it is not inside a dollar-quoted
    plpgsql body, a single-quoted literal, a line comment, or a block comment --
    all four of which legitimately contain semicolons. Splitting on every
    semicolon shreds function bodies and prose comments alike.
    """
    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(sql)

    def flush() -> None:
        statement = "".join(buffer).strip()
        if statement:
            statements.append(statement)
        buffer.clear()

    while index < length:
        # Line comment: copy through to end of line.
        if sql.startswith("--", index):
            end = sql.find("\n", index)
            end = length if end == -1 else end + 1
            buffer.append(sql[index:end])
            index = end
            continue

        # Block comment: copy through to the closing delimiter.
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            end = length if end == -1 else end + 2
            buffer.append(sql[index:end])
            index = end
            continue

        # Dollar-quoted body: copy through to the matching tag.
        match = re.match(r"\$[A-Za-z_]*\$", sql[index:])
        if match:
            tag = match.group(0)
            end = sql.find(tag, index + len(tag))
            end = length if end == -1 else end + len(tag)
            buffer.append(sql[index:end])
            index = end
            continue

        # Single-quoted literal: copy through, honouring '' escapes.
        if sql[index] == "'":
            cursor = index + 1
            while cursor < length:
                if sql[cursor] == "'":
                    if sql.startswith("''", cursor):
                        cursor += 2
                        continue
                    cursor += 1
                    break
                cursor += 1
            buffer.append(sql[index:cursor])
            index = cursor
            continue

        if sql[index] == ";":
            flush()
            index += 1
            continue

        buffer.append(sql[index])
        index += 1

    flush()
    return statements


def apply_sql_file(conn: psycopg.Connection, path: Path) -> int:
    sql = path.read_text(encoding="utf-8")
    statements = _split_statements(sql)
    with conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement)
    return len(statements)


def sync_dim_rule(conn: psycopg.Connection) -> int:
    """Mirror config/contract.json's rule registry into capstone.dim_rule.

    contract.json stays authoritative. This copy exists so Power BI can join to
    rule metadata without parsing JSON, and so fact_findings.rule_code has a
    real foreign key -- which is what makes an unregistered rule_code fail at
    load time instead of appearing as an unexplainable row on a dashboard.
    """
    rows = [
        (
            rule["rule_code"],
            rule["source_module"],
            rule["entity_type"],
            rule["default_severity"],
            rule["approval_required"],
            rule.get("zero_exposure_by_design", False),
            rule["exposure_basis"],
            CONTRACT["version"],
        )
        for rule in RULES_BY_CODE.values()
    ]

    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO capstone.dim_rule (
                   rule_code, source_module, entity_type, default_severity,
                   approval_required, zero_exposure_by_design, exposure_basis,
                   contract_version)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (rule_code) DO UPDATE SET
                   source_module = EXCLUDED.source_module,
                   entity_type = EXCLUDED.entity_type,
                   default_severity = EXCLUDED.default_severity,
                   approval_required = EXCLUDED.approval_required,
                   zero_exposure_by_design = EXCLUDED.zero_exposure_by_design,
                   exposure_basis = EXCLUDED.exposure_basis,
                   contract_version = EXCLUDED.contract_version""",
            rows,
        )
    return len(rows)


def _check_splitter() -> None:
    """Self-check for _split_statements. The parser is small but fiddly, and a
    mis-split silently truncates a function body or a CHECK constraint.
    """
    # A semicolon inside a line comment must not split the statement.
    assert len(_split_statements("SELECT 1; -- note; with semicolon\nSELECT 2;")) == 2
    assert len(_split_statements("-- only; a; comment\nSELECT 1;")) == 1

    # A plpgsql body's internal semicolons must stay inside one statement.
    body = """CREATE FUNCTION f() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.x <> 1 THEN
        RAISE EXCEPTION 'bad; very bad';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;"""
    assert len(_split_statements(body)) == 1, _split_statements(body)

    # A semicolon inside a string literal must not split.
    assert len(_split_statements("COMMENT ON TABLE t IS 'a; b'; SELECT 1;")) == 2

    # Escaped quotes must not end the literal early.
    assert len(_split_statements("SELECT 'it''s; fine';")) == 1

    # Block comments behave like line comments.
    assert len(_split_statements("/* a; b */ SELECT 1;")) == 1

    print("sql splitter self-check passed")


def apply_all() -> None:
    scripts = sorted(SQL_DIR.glob("[0-9][0-9]_*.sql"))
    if not scripts:
        raise SystemExit(f"no numbered sql scripts found in {SQL_DIR}")

    with connect() as conn:
        for script in scripts:
            count = apply_sql_file(conn, script)
            print(f"applied {script.name:<28} ({count} statements)")
        synced = sync_dim_rule(conn)
        print(f"synced dim_rule                  ({synced} rules)")
        conn.commit()


if __name__ == "__main__":
    _check_splitter()
    apply_all()
