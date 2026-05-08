import logging
import os
import threading
from datetime import datetime, timezone

from api.db import get_supabase
from brand_pipeline.brand_analysis import get_brand_profile
from brand_pipeline.day_part_templates import DAY_PART_TEMPLATES, get_day_part_hours, target_track_count
from brand_pipeline.sound_board import apply_segment_adjustments, get_sound_board
from pipeline.mmr_scorer import mmr_select
from pipeline.rag_retriever import (
    fetch_must_include_genre_tracks,
    fetch_must_include_tracks,
    retrieve_candidates,
)

from api.activity import log_activity
from api.services.ai_service import chat_client
from api.store import get_brands, get_playlists, save_brands, save_playlists
from api.tasks import get_task, new_task, task_done, task_error, task_log, task_progress
from api.utils import fmt_daypart, pipeline_inputs


logger = logging.getLogger(__name__)

MMR_LAMBDA = float(os.getenv("MMR_LAMBDA", "0.7"))


def _apply_genre_overrides(inputs: dict, genre_overrides: dict | None) -> dict:
    if not genre_overrides:
        return inputs
    overrides_include = genre_overrides.get("include", [])
    overrides_exclude = genre_overrides.get("exclude", [])
    stored_include = [g.strip() for g in (inputs.get("include_genres") or "").split(",") if g.strip()]
    stored_exclude = [g.strip() for g in (inputs.get("exclude_genres") or "").split(",") if g.strip()]
    merged_include = [g for g in dict.fromkeys(stored_include + overrides_include) if g not in overrides_exclude]
    merged_exclude = list(dict.fromkeys(stored_exclude + overrides_exclude))
    updated = dict(inputs)
    updated["include_genres"] = ", ".join(merged_include)
    updated["exclude_genres"] = ", ".join(merged_exclude)
    return updated


def _resolved_category(raw_category: str) -> str:
    return raw_category if raw_category in DAY_PART_TEMPLATES else "cafe"


def _sync_playlist_artifacts(brand_id: str, user_id: str | None, playlist_result: dict, assembled: list[dict]) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        get_supabase().table("brand_playlists").upsert(
            {
                "brand_id": brand_id,
                "user_id": user_id,
                "playlist_json": playlist_result,
                "updated_at": now_iso,
            },
            on_conflict="brand_id",
        ).execute()
    except Exception as exc:
        logger.warning("Could not save playlist to brand_playlists: %s", exc)

    if not user_id:
        return

    unique_songs: dict[str, str] = {}
    for day_part_result in assembled:
        for track in day_part_result.get("tracks", []):
            song_id = track.get("song_id")
            if song_id and song_id not in unique_songs:
                unique_songs[song_id] = track.get("title", "")

    if not unique_songs:
        return

    try:
        get_supabase().table("user_playlist_songs").delete().eq("user_id", user_id).eq("brand_id", brand_id).execute()
        rows = [
            {
                "user_id": user_id,
                "brand_id": brand_id,
                "song_id": song_id,
                "song_name": song_name,
                "generated_at": now_iso,
            }
            for song_id, song_name in unique_songs.items()
        ]
        get_supabase().table("user_playlist_songs").insert(rows).execute()
    except Exception as exc:
        logger.warning("Could not sync playlist songs to Supabase: %s", exc)


def _bg_soundboard(brand_id: str, task_id: str) -> None:
    try:
        brands = get_brands()
        brand = brands.get(brand_id)
        if not brand:
            task_error(task_id, f"Brand {brand_id} not found")
            return

        task_progress(task_id, 10, "Analysing brand identity...")
        client, deployment = chat_client()
        inputs = pipeline_inputs(brand)

        brand_profile = get_brand_profile(inputs, client, deployment)
        brand_profile.setdefault("brand_name", brand["brand_name"])
        brand_profile.setdefault("customer_description", brand.get("customer_description", ""))
        brand_profile.setdefault("price_positioning", brand.get("customer_segment", "mid_range"))

        task_progress(task_id, 55, "Generating sound board parameters...")
        category = _resolved_category(brand.get("category") or "cafe")
        sound_board = get_sound_board(
            brand_profile,
            category,
            client,
            deployment,
            music_notes=brand.get("music_notes", ""),
        )
        sound_board = apply_segment_adjustments(sound_board, brand.get("customer_segment", "mid_range"))

        brands = get_brands()
        if brand_id not in brands:
            task_error(task_id, f"Brand {brand_id} not found")
            return
        brands[brand_id].update(
            {
                "brand_profile": brand_profile,
                "sound_board_result": sound_board,
                "status": "active",
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
        )
        save_brands(brands)
        log_activity("Sound board generated", brand["brand_name"])
        task_progress(task_id, 100, "Sound board ready.")
        task_done(task_id)
    except Exception as exc:
        logger.exception("Sound board failed")
        task_error(task_id, str(exc))


def _bg_playlist(brand_id: str, task_id: str, genre_overrides: dict | None = None, playlist_name: str | None = None) -> None:
    try:
        brands = get_brands()
        brand = brands.get(brand_id)
        if not brand:
            task_error(task_id, f"Brand {brand_id} not found")
            return

        brand_profile = brand.get("brand_profile")
        sound_board_result = brand.get("sound_board_result")

        if not brand_profile or not sound_board_result:
            task_progress(task_id, 5, "Generating brand profile...")
            client, deployment = chat_client()
            inputs = _apply_genre_overrides(pipeline_inputs(brand), genre_overrides)
            brand_profile = get_brand_profile(inputs, client, deployment)
            brand_profile.setdefault("brand_name", brand["brand_name"])
            brand_profile.setdefault("customer_description", brand.get("customer_description", ""))
            brand_profile.setdefault("price_positioning", brand.get("customer_segment", "mid_range"))

            task_progress(task_id, 20, "Generating sound board...")
            category = _resolved_category(brand.get("category") or "cafe")
            sound_board_result = get_sound_board(
                brand_profile,
                category,
                client,
                deployment,
                music_notes=brand.get("music_notes", ""),
            )
            sound_board_result = apply_segment_adjustments(
                sound_board_result,
                brand.get("customer_segment", "mid_range"),
            )

            brands = get_brands()
            if brand_id not in brands:
                task_error(task_id, f"Brand {brand_id} not found")
                return
            brands[brand_id]["brand_profile"] = brand_profile
            brands[brand_id]["sound_board_result"] = sound_board_result
            save_brands(brands)

        task_progress(task_id, 30, "Starting MMR selection...")
        inputs = _apply_genre_overrides(pipeline_inputs(brand), genre_overrides)
        day_parts = sound_board_result.get("day_parts", [])

        include_artists = [item.strip() for item in (inputs.get("include_artists") or "").split(",") if item.strip()]
        include_genres = [item.strip() for item in (genre_overrides or {}).get("include", []) if item.strip()]
        if include_genres:
            task_log(task_id, f"  Genre overrides (include): {', '.join(include_genres)}")

        assembled: list[dict] = []
        total_parts = max(len(day_parts), 1)
        used_song_ids: set = set()

        for index, day_part in enumerate(day_parts):
            day_part_name = day_part.get("name", f"Day-Part {index + 1}")
            pct = 30 + int((index / total_parts) * 65)
            task_progress(task_id, pct, f"Running MMR for {day_part_name}...")

            candidates, all_playable, stats = retrieve_candidates(
                brand_profile=brand_profile,
                day_part_params=day_part,
                inputs=inputs,
                used_song_ids=used_song_ids,
            )

            if not candidates:
                task_log(task_id, f"  {day_part_name}: skipped (no candidates found)")
                assembled.append(fmt_daypart(day_part, []))
                continue
            if stats.get("skipped"):
                task_log(task_id, f"  {day_part_name}: few candidates ({len(candidates)}), proceeding...")

            fresh_candidates = [candidate for candidate in candidates if candidate.get("song_id") not in used_song_ids]
            if len(fresh_candidates) >= 5:
                candidates = fresh_candidates
                task_log(task_id, f"  {day_part_name}: {len(candidates)} fresh candidates after cross-part dedup")
            else:
                task_log(task_id, f"  {day_part_name}: catalog too small for full dedup, reusing pool")

            target_count = target_track_count(day_part)
            must_include = fetch_must_include_tracks(include_artists, candidates, target_count)
            genre_injections = fetch_must_include_genre_tracks(include_genres, candidates, target_count)

            if genre_injections:
                task_log(
                    task_id,
                    f"  {day_part_name}: injecting {len(genre_injections)} genre track(s): "
                    + ", ".join(track.get("title", "?") for track in genre_injections),
                )
            elif include_genres:
                task_log(task_id, f"  {day_part_name}: no tracks found in DB for genres: {', '.join(include_genres)}")

            genre_ids = {track.get("song_id") for track in candidates}
            for track in genre_injections:
                if track.get("song_id") not in genre_ids:
                    candidates.append(track)
                    genre_ids.add(track.get("song_id"))

            must_include = list({track.get("song_id"): track for track in must_include + genre_injections}.values())
            target_seconds = get_day_part_hours(day_part) * 3600 * 1.25
            candidate_ids = {track.get("song_id") for track in candidates}
            spillover = [
                track
                for track in all_playable
                if track.get("song_id") not in candidate_ids and track.get("song_id") not in used_song_ids
            ]

            playlist = mmr_select(
                candidates=candidates,
                must_include=must_include,
                dp=day_part,
                target_count=target_count,
                target_seconds=target_seconds,
                lam=MMR_LAMBDA,
                spillover=spillover,
            )

            for track in playlist:
                song_id = track.get("song_id")
                if song_id:
                    used_song_ids.add(song_id)

            if playlist:
                task_log(task_id, f"  {day_part_name}: {len(playlist)} tracks selected (MMR lambda={MMR_LAMBDA})")
            else:
                task_log(task_id, f"  {day_part_name}: 0 tracks")

            assembled.append(fmt_daypart(day_part, playlist))

        sound_board_summary = sound_board_result.get("sound_board", {})
        playlist_result = {
            "brand_profile": brand_profile,
            "sound_board": {
                "brand_summary": brand_profile.get("brand_summary", ""),
                "primary_genres": sound_board_summary.get("primary_genres", []),
                "secondary_genres": sound_board_summary.get("secondary_genres", []),
                "energy_target": sound_board_summary.get("energy_target", 0.5),
                "valence_target": sound_board_summary.get("valence_target", 0.5),
                "tempo_target": sound_board_summary.get("tempo_target", 110),
                "danceability_target": sound_board_summary.get("danceability_target", 0.5),
                "acousticness_target": sound_board_summary.get("acousticness_target", 0.4),
                "instrumentalness_target": sound_board_summary.get("instrumentalness_target", 0.3),
                "loudness_target": 0.5,
                "speechiness_target": 0.2,
            },
            "day_parts": assembled,
        }

        playlists = get_playlists()
        playlists[brand_id] = playlist_result
        save_playlists(playlists)

        user_id = brand.get("user_id")
        _sync_playlist_artifacts(brand_id, user_id, playlist_result, assembled)

        brands = get_brands()
        if brand_id not in brands:
            task_error(task_id, f"Brand {brand_id} not found")
            return
        is_first_playlist = brands[brand_id].get("playlist_count", 0) == 0
        now_iso = datetime.now(timezone.utc).isoformat()
        brands[brand_id]["playlist_count"] = brands[brand_id].get("playlist_count", 0) + 1
        brands[brand_id]["last_updated"] = now_iso
        brands[brand_id]["last_generated_at"] = now_iso
        brands[brand_id]["status"] = "active"
        if playlist_name and is_first_playlist:
            brands[brand_id]["playlist_name"] = playlist_name.strip()
        elif not brands[brand_id].get("playlist_name"):
            brands[brand_id]["playlist_name"] = brands[brand_id].get("brand_name", "")
        save_brands(brands)

        log_activity("Playlists generated (MMR)", brand["brand_name"])
        task_progress(task_id, 100, "All playlists ready.")
        task_done(task_id)
    except Exception as exc:
        logger.exception("Playlist generation failed")
        task_error(task_id, str(exc))


def start_soundboard(brand_id: str) -> tuple[str | None, str | None, int | None]:
    if brand_id not in get_brands():
        return None, "Brand not found", 404
    task_id = new_task()
    threading.Thread(target=_bg_soundboard, args=(brand_id, task_id), daemon=True).start()
    return task_id, None, None


def start_playlist_generation(brand_id: str, genre_overrides: dict | None = None, playlist_name: str | None = None) -> tuple[str | None, str | None, int | None]:
    if brand_id not in get_brands():
        return None, "Brand not found", 404
    task_id = new_task()
    threading.Thread(
        target=_bg_playlist,
        args=(brand_id, task_id, genre_overrides or {"include": [], "exclude": []}, playlist_name),
        daemon=True,
    ).start()
    return task_id, None, None


def get_generation_task(task_id: str) -> tuple[dict | None, str | None, int | None]:
    task = get_task(task_id)
    if not task:
        return None, "Task not found", 404
    return task, None, None
