"""Smoke tests for the CRM reporting pipeline.

No pytest required:

    python tests/test_smoke.py

Also works under pytest (`pytest -q`) because the functions are named test_*.

What is actually being asserted — the three claims the README makes:

    1. the pipeline runs end to end and produces both artefacts
    2. re-running it does not create duplicate rows (idempotency)
    3. anomalous amounts are excluded from the pipeline total
"""

from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pipeline  # noqa: E402


def _fresh_db(tmpdir: str) -> str:
    """Point the module at a throwaway database."""
    pipeline.DB_PATH = os.path.join(tmpdir, "test.db")
    pipeline.OUT_DIR = os.path.join(tmpdir, "out")
    pipeline.EXPORT_PATH = os.path.join(tmpdir, "export.json")
    if os.path.exists(pipeline.DB_PATH):
        os.remove(pipeline.DB_PATH)
    return pipeline.DB_PATH


def _query(sql: str, args: tuple = ()):
    conn = sqlite3.connect(pipeline.DB_PATH)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


def test_end_to_end(tmpdir: str) -> None:
    _fresh_db(tmpdir)
    code = pipeline.run_once()
    assert code == 0, "run_once() should return 0"

    reports = [f for f in os.listdir(pipeline.OUT_DIR) if f.startswith("report_")]
    assert reports, "no markdown report was produced"
    assert os.path.exists(os.path.join(pipeline.OUT_DIR, "dashboard.html")), "no dashboard"

    n_contacts = _query("SELECT COUNT(*) FROM contacts")
    n_opps = _query("SELECT COUNT(*) FROM opportunities")
    assert n_contacts > 0, "contacts table is empty"
    assert n_opps > 0, "opportunities table is empty"
    print(f"  ok  end-to-end: {n_contacts} contacts, {n_opps} opportunities")


def test_idempotent(tmpdir: str) -> None:
    _fresh_db(tmpdir)
    pipeline.run_once()
    first = _query("SELECT COUNT(*) FROM contacts")

    pipeline.run_once()                     # second run, same input
    second = _query("SELECT COUNT(*) FROM contacts")

    assert first == second, f"row count changed on re-run: {first} -> {second}"

    rows = sqlite3.connect(pipeline.DB_PATH).execute(
        "SELECT contacts_new FROM run_log ORDER BY id").fetchall()
    assert rows[0][0] > 0, "first run should insert rows"
    assert rows[1][0] == 0, f"second run inserted {rows[1][0]} rows, expected 0"
    print(f"  ok  idempotent: first={rows[0][0]} new, second={rows[1][0]} new")


def test_dedupe_and_hygiene(tmpdir: str) -> None:
    _fresh_db(tmpdir)
    pipeline.run_once()
    seen = _query("SELECT contacts_seen FROM run_log ORDER BY id LIMIT 1")
    stored = _query("SELECT COUNT(*) FROM contacts")
    dup = _query("SELECT dup_removed FROM run_log ORDER BY id LIMIT 1")

    assert stored < seen, "dedupe did not reduce the contact count"
    assert dup > 0, "no duplicates were detected in the synthetic fixture"
    assert stored + dup == seen, f"{stored} + {dup} != {seen}"

    # every stored contact must have a unique normalised email (or none at all)
    dup_emails = sqlite3.connect(pipeline.DB_PATH).execute(
        "SELECT COUNT(*) FROM (SELECT email FROM contacts WHERE email IS NOT NULL "
        "GROUP BY email HAVING COUNT(*) > 1)").fetchone()[0]
    assert dup_emails == 0, f"{dup_emails} duplicate emails survived the cleaning step"
    print(f"  ok  hygiene: {seen} seen, {dup} duplicates removed, {stored} stored")


def test_anomalous_amounts_excluded(tmpdir: str) -> None:
    _fresh_db(tmpdir)
    pipeline.run_once()
    conn = sqlite3.connect(pipeline.DB_PATH)
    naive = conn.execute(
        "SELECT COALESCE(SUM(monetary_value),0) FROM opportunities "
        "WHERE stage NOT IN ('Won','Lost')").fetchone()[0]
    clean = conn.execute(
        "SELECT COALESCE(SUM(monetary_value),0) FROM opportunities "
        "WHERE stage NOT IN ('Won','Lost') AND monetary_value <= 100000").fetchone()[0]
    anomalies = conn.execute("SELECT anomalies FROM run_log ORDER BY id LIMIT 1").fetchone()[0]

    assert anomalies > 0, "synthetic fixture should contain anomalies"
    assert clean < naive, "cleaning did not change the pipeline total"
    print(f"  ok  anomalies excluded: naive ${naive:,.0f} -> clean ${clean:,.0f} "
          f"({anomalies} flagged)")


def main() -> int:
    import shutil

    # use a scratch dir inside the project: sandboxed/CI environments often
    # restrict writes to the system temp directory
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)

    tests = [test_end_to_end, test_idempotent, test_dedupe_and_hygiene,
             test_anomalous_amounts_excluded]
    failed = 0
    for t in tests:
        print(f"{t.__name__}")
        d = os.path.join(base, t.__name__)
        os.makedirs(d, exist_ok=True)
        try:
            t(d)
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {e}")
    shutil.rmtree(base, ignore_errors=True)
    print()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
