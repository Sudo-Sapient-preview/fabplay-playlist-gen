"""One-time backfill of song_mood_labels from pipeline_output->mood_top_labels.

pipeline_output is a large TOASTed jsonb column, so this runs in small batches
to stay under the 2-minute statement timeout. Safe to re-run (idempotent).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sql import run_sql  # noqa: E402

BATCH = 500


def main() -> None:
    total = run_sql("select count(*) c from songs_metadata_table_sample")[0]["c"]
    done = run_sql("select count(*) c from song_mood_labels")[0]["c"]
    print(f"catalog rows: {total} | already backfilled: {done}")

    started = time.time()
    while True:
        rows = run_sql(
            f"""
            with todo as (
                select m.id, m.pipeline_output->'mood_top_labels' as mood
                from songs_metadata_table_sample m
                left join song_mood_labels ml on ml.id = m.id
                where ml.id is null
                order by m.id
                limit {BATCH}
            ), ins as (
                insert into song_mood_labels (id, mood_labels)
                select t.id,
                       case
                         when jsonb_typeof(t.mood) = 'array' then
                           coalesce(
                             (select array_agg(x::text)
                                from jsonb_array_elements_text(t.mood) x),
                             '{{}}'::text[])
                         else '{{}}'::text[]
                       end
                from todo t
                on conflict (id) do nothing
                returning 1
            )
            select count(*) n from ins;
            """
        )
        n = rows[0]["n"]
        done += n
        pct = done * 100.0 / total if total else 100.0
        print(f"  +{n:<4} -> {done}/{total} ({pct:.1f}%)  {time.time()-started:.0f}s", flush=True)
        if n == 0:
            break

    print("BACKFILL COMPLETE")


if __name__ == "__main__":
    main()
