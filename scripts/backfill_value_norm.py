"""One-off backfill: recompute value_norm/unit_norm for stored numeric facts
that were extracted before the 'per cent' unit alias existed (value_norm NULL
disables verification and tolerance checks). Deterministic — no LLM calls.
Usage: DATA_DIR=data_macro python -m scripts.backfill_value_norm
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import store as S
from app.normalize import normalize_fact_value


def main() -> None:
    con = S.connect()
    rows = con.execute(
        "SELECT fact_id, value_raw, unit_norm, quote FROM facts "
        "WHERE fact_type='numeric' AND value_norm IS NULL").fetchall()
    fixed = 0
    for fid, raw, unit, quote in rows:
        base, unorm, _, _ = normalize_fact_value(raw or "", unit or "", quote or "")
        if base is not None:
            con.execute("UPDATE facts SET value_norm=?, unit_norm=? WHERE fact_id=?",
                        (base, unorm, fid))
            fixed += 1
    con.commit()
    print(f"backfilled {fixed}/{len(rows)} null-value numeric facts")
    con.close()


if __name__ == "__main__":
    main()
