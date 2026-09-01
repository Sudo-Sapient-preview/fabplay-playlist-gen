"""Delete the verified-silent tracks from Supabase Storage and both source tables.

Run tools/verify_silence.py first: it decodes every candidate with ffmpeg and only
writes silence_ids.txt when all of them measure <= -60 dBFS peak.

Order matters: storage objects are removed first, then DB rows. If storage fails
we stop before touching the database, so the two never drift apart.

Usage:
    python tools/delete_silence.py           # dry run, shows what would happen
    python tools/delete_silence.py --apply   # actually delete
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv  # noqa: E402
from sql import run_sql  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
URL = os.environ["SUPABASE_URL"]
KEY = os.environ["SUPABASE_SERVICE_KEY"]
BUCKET = "songs"
APPLY = "--apply" in sys.argv

ids = [int(x) for x in Path("silence_ids.txt").read_text().split()]
id_list = ",".join(map(str, ids))
print(f"{len(ids)} track(s) marked for deletion: {ids}\n")

# Guard: refuse to run if any target still has audible sound recorded in metadata.
guard = run_sql(
    f"select count(*) n from public.songs_metadata_table_sample "
    f"where id in ({id_list}) and loudness <> 0;"
)
if guard[0]["n"]:
    sys.exit(f"ABORT: {guard[0]['n']} target row(s) have loudness <> 0.")

paths = [r["storage_path"] or f"{r['id']}.mp3"
         for r in run_sql(f"select id, storage_path from public.songs where id in ({id_list}) order by id;")]

if not APPLY:
    print("DRY RUN — nothing will be changed. Re-run with --apply to execute.")
    print(f"  storage objects to remove : {len(paths)}")
    for t in ("songs", "songs_metadata_table_sample", "song_mood_labels"):
        n = run_sql(f"select count(*) n from public.{t} where id in ({id_list});")[0]["n"]
        print(f"  rows to delete in {t:<28}: {n}")
    sys.exit(0)

# 1. Storage first — if this fails we have not yet touched the DB.
req = urllib.request.Request(
    f"{URL}/storage/v1/object/{BUCKET}",
    data=json.dumps({"prefixes": paths}).encode(),
    headers={"apikey": KEY, "Authorization": f"Bearer {KEY}",
             "Content-Type": "application/json"},
    method="DELETE",
)
with urllib.request.urlopen(req) as resp:
    removed = json.load(resp)
print(f"storage: removed {len(removed)} object(s)")

# 2. Database rows (child table first).
for table in ("song_mood_labels", "songs_metadata_table_sample", "songs"):
    run_sql(f"delete from public.{table} where id in ({id_list});")
    left = run_sql(f"select count(*) n from public.{table} where id in ({id_list});")[0]["n"]
    print(f"{table:<30} remaining target rows: {left}")

print("\nDeletion complete.")
