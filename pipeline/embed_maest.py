"""
embed_maest.py — Generate MAEST audio-to-audio embeddings for the catalog.

MAEST (Music Audio Spectrogram Transformer) is trained for music-to-music similarity.
These embeddings power the "suggest similar songs" feature.

Run once after initial setup, then again whenever new songs are added.

Usage:
    python -m pipeline.embed_maest
    python -m pipeline.embed_maest --force          # re-embed songs that already have embeddings
    python -m pipeline.embed_maest --limit 100      # process only first 100 songs (for testing)
    python -m pipeline.embed_maest --batch-size 32  # DB upsert batch size (default: 50)

Requirements (install separately — not part of the Django web server):
    pip install -r requirements-embed.txt
"""

import argparse
import io
import logging
import os
import sys

# Django must be configured before any app imports.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fabplay.settings")
import django
django.setup()

import librosa
import numpy as np
import requests
import torch
from maest import get_maest

from api.db import fetch_all_songs, get_supabase, upsert_maest_audio_768s

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

_MAEST_SAMPLE_RATE = 16000  # MAEST expects 16 kHz mono audio
_DOWNLOAD_TIMEOUT = 30      # seconds
_DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _download_audio(url: str) -> bytes | None:
    try:
        resp = requests.get(url, timeout=_DOWNLOAD_TIMEOUT, stream=True)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        logger.warning("Download failed for %s: %s", url, exc)
        return None


def _load_audio_array(audio_bytes: bytes) -> np.ndarray | None:
    """Decode audio bytes → mono float32 numpy array at _MAEST_SAMPLE_RATE."""
    try:
        buf = io.BytesIO(audio_bytes)
        audio, _ = librosa.load(buf, sr=_MAEST_SAMPLE_RATE, mono=True)
        return audio  # float32 ndarray, shape (num_samples,)
    except Exception as exc:
        logger.warning("Audio decode failed: %s", exc)
        return None


def _embed(model, audio: np.ndarray) -> list[float] | None:
    """Run MAEST inference → 400-dim Discogs style-tag activation vector.

    MAEST's output logits represent 400 Discogs music style predictions.
    Songs with similar sonic character produce similar activation patterns,
    making this a valid audio-to-audio similarity embedding.
    """
    try:
        audio_tensor = torch.from_numpy(audio).float().to(_DEVICE)
        with torch.no_grad():
            output = model(audio_tensor)
        if isinstance(output, (tuple, list)):
            output = output[0]
        if isinstance(output, torch.Tensor):
            return output.squeeze().cpu().tolist()
        return [float(v) for v in output]
    except Exception as exc:
        logger.warning("MAEST inference failed: %s", exc)
        return None


def _fetch_already_embedded() -> set[str]:
    resp = (
        get_supabase()
        .table("analysis_song_features")
        .select("song_id")
        .not_.is_("maest_audio_768", "null")
        .execute()
    )
    return {row["song_id"] for row in (resp.data or [])}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate MAEST embeddings for all catalog songs.")
    parser.add_argument("--force", action="store_true", help="Re-embed songs that already have a maest_audio_768.")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N songs (0 = unlimited).")
    parser.add_argument("--batch-size", type=int, default=50, help="DB upsert batch size.")
    args = parser.parse_args()

    logger.info("Loading MAEST model (discogs-maest-30s-pw-129e) on %s…", _DEVICE)
    model = get_maest(arch="discogs-maest-30s-pw-129e")
    model.to(_DEVICE)
    model.eval()
    logger.info("MAEST model ready on %s.", _DEVICE)

    all_songs = fetch_all_songs()

    if args.force:
        songs = all_songs
        logger.info("--force: processing all %d songs.", len(songs))
    else:
        done = _fetch_already_embedded()
        songs = [s for s in all_songs if str(s.get("id", "")) not in done]
        logger.info("%d / %d songs need embedding.", len(songs), len(all_songs))

    if args.limit:
        songs = songs[: args.limit]
        logger.info("--limit: capped at %d songs.", len(songs))

    total = len(songs)
    pending: list[dict] = []
    ok, fail = 0, 0

    for i, song in enumerate(songs, 1):
        song_id = str(song.get("id", ""))
        url = str(song.get("url") or "").strip()
        label = f"[{i}/{total}] '{song.get('title', song_id)}'"

        if not url.startswith("http"):
            logger.warning("%s — skipped (no HTTP URL)", label)
            fail += 1
            continue

        audio_bytes = _download_audio(url)
        if not audio_bytes:
            fail += 1
            continue

        audio = _load_audio_array(audio_bytes)
        if audio is None:
            fail += 1
            continue

        embedding = _embed(model, audio)
        if embedding is None:
            fail += 1
            continue

        pending.append({"song_id": song_id, "maest_audio_768": embedding})
        ok += 1
        logger.info("%s — embedded (%d-dim)", label, len(embedding))

        if len(pending) >= args.batch_size:
            upsert_maest_audio_768s(pending)
            logger.info("Upserted batch of %d to DB.", len(pending))
            pending.clear()

    if pending:
        upsert_maest_audio_768s(pending)
        logger.info("Upserted final batch of %d to DB.", len(pending))

    logger.info("Done. Embedded: %d  Failed/skipped: %d  Total: %d", ok, fail, total)


if __name__ == "__main__":
    main()
