"""Decode every loudness=0 track with ffmpeg and prove whether it is truly silent.

Metadata alone is not trustworthy: a byte-level check on MP3 data is meaningless
because compressed frames encoding silence still contain non-zero bytes. The only
reliable test is to actually decode the audio and measure peak amplitude.

Verdict is SILENT only if max_volume <= -60 dBFS.
"""
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sql import run_sql  # noqa: E402

BASE = "https://iruimptvnjrpmgtebjzq.supabase.co/storage/v1/object/public/songs"
SILENCE_DB = -60.0

rows = run_sql(
    """
    select s.id, m.title, s.label as library, s.storage_path,
           round(m.duration_seconds::numeric, 1) as dur
    from public.songs s
    join public.songs_metadata_table_sample m on m.id = s.id
    where m.loudness = 0
    order by s.id;
    """
)

print(f"Decoding {len(rows)} tracks with ffmpeg (max_volume <= {SILENCE_DB} dB => SILENT)\n")
print(f"{'ID':<7}{'TITLE':<20}{'LIBRARY':<19}{'DUR':>8}{'MAX_dB':>9}{'MEAN_dB':>9}  VERDICT")
print("-" * 92)

silent, has_audio, failed = [], [], []
tmp = Path(tempfile.gettempdir())

for r in rows:
    sid = r["id"]
    path = r["storage_path"] or f"{sid}.mp3"
    dest = tmp / f"verify_{sid}.mp3"
    title = " ".join(str(r["title"]).split())[:19]
    try:
        urllib.request.urlretrieve(f"{BASE}/{path}", dest)
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(dest),
             "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=180,
        ).stderr
        mx = re.search(r"max_volume:\s*(-?[\d.]+)", out)
        mn = re.search(r"mean_volume:\s*(-?[\d.]+)", out)
        if not mx:
            failed.append(sid)
            print(f"{sid:<7}{title:<20}{r['library']:<19}{r['dur']:>8}{'DECODE FAILED':>20}")
            continue
        mxv, mnv = float(mx.group(1)), float(mn.group(1)) if mn else 0.0
        is_silent = mxv <= SILENCE_DB
        (silent if is_silent else has_audio).append(sid)
        verdict = "SILENT" if is_silent else "*** HAS AUDIO — DO NOT DELETE ***"
        print(f"{sid:<7}{title:<20}{r['library']:<19}{r['dur']:>8}{mxv:>9.1f}{mnv:>9.1f}  {verdict}")
    except Exception as exc:
        failed.append(sid)
        print(f"{sid:<7}{title:<20}{r['library']:<19}{r['dur']:>8}   ERROR {exc}")
    finally:
        dest.unlink(missing_ok=True)

print("-" * 92)
print(f"SILENT   : {len(silent)} -> {silent}")
print(f"HAS AUDIO: {len(has_audio)} -> {has_audio}")
print(f"FAILED   : {len(failed)} -> {failed}")

if has_audio or failed:
    print("\nNOT SAFE to delete blindly: some tracks contain audio or could not be verified.")
    sys.exit(1)

Path("silence_ids.txt").write_text("\n".join(str(i) for i in silent))
print(f"\nAll {len(silent)} verified silent. IDs written to silence_ids.txt")
