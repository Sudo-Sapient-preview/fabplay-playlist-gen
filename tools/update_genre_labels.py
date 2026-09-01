"""Update songs_metadata_table_sample.genre_label from fabplay_all_songs_alldata.xlsx.

Matches Excel `id` -> DB `id`, copies Excel `genre_title` into `genre_label`
(normalised to lowercase snake_case to match app genre conventions).
IDs present only in Excel are skipped.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sql import run_sql  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXCEL = ROOT / "fabplay_all_songs_alldata.xlsx"
BATCH = 500


def norm_genre(value) -> str | None:
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none"}:
        return None
    s = re.sub(r"[\s/]+", "_", s.lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or None


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main() -> None:
    print(f"Reading {EXCEL} ...")
    df = pd.read_excel(EXCEL, usecols=["id", "genre_title"])
    df["id"] = df["id"].astype(int)
    df["genre_norm"] = df["genre_title"].map(norm_genre)
    df = df.dropna(subset=["genre_norm"]).drop_duplicates(subset=["id"], keep="last")
    excel_map = dict(zip(df["id"], df["genre_norm"]))
    print(f"Excel rows with genre: {len(excel_map)}")
    print("Genre distribution:")
    print(df["genre_norm"].value_counts().to_string())

    db_rows = run_sql("select id, genre_label from songs_metadata_table_sample")
    print(f"\nDB rows: {len(db_rows)}")

    updates: list[tuple[int, str]] = []
    same = 0
    skipped_no_excel = 0
    for row in db_rows:
        song_id = int(row["id"])
        new_genre = excel_map.get(song_id)
        if new_genre is None:
            skipped_no_excel += 1
            continue
        old_genre = row["genre_label"] or ""
        if old_genre == new_genre:
            same += 1
        else:
            updates.append((song_id, new_genre))

    print(
        f"Will update: {len(updates)} | already same: {same} | "
        f"db without excel match: {skipped_no_excel} | "
        f"excel-only ids skipped: {len(excel_map) - (len(updates) + same)}"
    )
    if not updates:
        print("Nothing to update.")
        return

    started = time.time()
    updated = 0
    for i in range(0, len(updates), BATCH):
        batch = updates[i : i + BATCH]
        values = ",\n".join(f"({song_id}, {sql_quote(genre)})" for song_id, genre in batch)
        sql = f"""
        update songs_metadata_table_sample as m
        set genre_label = v.genre_label
        from (values
            {values}
        ) as v(id, genre_label)
        where m.id = v.id::bigint;
        """
        run_sql(sql)
        updated += len(batch)
        print(
            f"  updated {updated}/{len(updates)} "
            f"({updated * 100 / len(updates):.1f}%)  {time.time() - started:.0f}s",
            flush=True,
        )

    print("\nVerifying ...")
    summary = run_sql(
        """
        select genre_label, count(*) as c
        from songs_metadata_table_sample
        group by genre_label
        order by c desc
        """
    )
    for row in summary:
        print(f"  {row['genre_label']}: {row['c']}")

    sample = run_sql(
        "select id, title, genre_label from songs_metadata_table_sample order by id limit 10"
    )
    print("\nSample after update:")
    for row in sample:
        print(f"  {row['id']}: {row['title']!r} -> {row['genre_label']}")

    print("\nUPDATE COMPLETE")


if __name__ == "__main__":
    main()
