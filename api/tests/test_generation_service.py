import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

import api.store as store
import api.tasks as tasks
from api.services import generation_service


class GenerationServiceTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self._old_brands_file = store.BRANDS_FILE
        store.BRANDS_FILE = self.tmp_path / "brands.json"
        store.save_brands({})
        tasks._tasks.clear()

    def tearDown(self):
        store.BRANDS_FILE = self._old_brands_file
        tasks._tasks.clear()
        self._tmpdir.cleanup()
        super().tearDown()

    def test_apply_genre_overrides_merges_and_excludes_cleanly(self):
        inputs = {
            "include_genres": "pop, jazz",
            "exclude_genres": "metal",
        }

        updated = generation_service._apply_genre_overrides(
            inputs,
            {"include": ["ambient", "pop"], "exclude": ["jazz", "metal"]},
        )

        self.assertEqual(updated["include_genres"], "pop, ambient")
        self.assertEqual(updated["exclude_genres"], "metal, jazz")

    def test_start_playlist_generation_spawns_background_thread(self):
        store.save_brands({"brand-1": {"id": "brand-1"}})
        mock_thread = MagicMock()

        with patch("api.services.generation_service.new_task", return_value="task-123"), patch(
            "api.services.generation_service.threading.Thread",
            return_value=mock_thread,
        ) as thread_cls:
            task_id, message, status = generation_service.start_playlist_generation(
                "brand-1",
                genre_overrides=None,
                playlist_name="Evening",
            )

        self.assertEqual((task_id, message, status), ("task-123", None, None))
        thread_cls.assert_called_once()
        kwargs = thread_cls.call_args.kwargs
        self.assertEqual(kwargs["target"], generation_service._bg_playlist)
        self.assertEqual(
            kwargs["args"],
            ("brand-1", "task-123", {"include": [], "exclude": []}, "Evening"),
        )
        self.assertTrue(kwargs["daemon"])
        mock_thread.start.assert_called_once()

    def test_bg_playlist_builds_first_playlist_and_marks_task_done(self):
        store.save_brands(
            {
                "brand-1": {
                    "id": "brand-1",
                    "user_id": "user-1",
                    "brand_name": "Fab Cafe",
                    "category": "cafe",
                    "customer_segment": "mid_range",
                    "playlist_count": 0,
                    "include_genres": ["pop"],
                    "exclude_genres": [],
                }
            }
        )
        task_id = tasks.new_task()
        candidate = {
            "song_id": "song-1",
            "title": "Track 1",
            "library": "Epic",
            "genre": "pop",
            "url": "songs/demo.mp3",
            "tempo_bpm": 108,
            "mmr_score": 0.61,
            "relevance_score": 0.74,
            "duration_seconds": 180,
        }
        sound_board = {
            "sound_board": {
                "primary_genres": ["pop"],
                "secondary_genres": ["ambient"],
                "energy_target": 0.6,
                "valence_target": 0.5,
                "tempo_target": 108,
                "danceability_target": 0.5,
                "acousticness_target": 0.3,
                "instrumentalness_target": 0.2,
            },
            "day_parts": [
                {
                    "name": "Morning",
                    "start_time": "06:00",
                    "end_time": "11:00",
                    "genre_emphasis": ["pop"],
                }
            ],
        }

        with patch("api.services.generation_service.chat_client", return_value=("client", "deployment")), patch(
            "api.services.generation_service.get_brand_profile",
            return_value={"brand_summary": "Warm and bright"},
        ), patch(
            "api.services.generation_service.get_sound_board",
            return_value=sound_board,
        ), patch(
            "api.services.generation_service.apply_segment_adjustments",
            side_effect=lambda result, _segment: result,
        ), patch(
            "api.services.generation_service.retrieve_candidates",
            return_value=([candidate], [candidate], {}),
        ), patch(
            "api.services.generation_service.fetch_must_include_tracks",
            return_value=[],
        ), patch(
            "api.services.generation_service.fetch_must_include_genre_tracks",
            return_value=[],
        ), patch(
            "api.services.generation_service.target_track_count",
            return_value=1,
        ), patch(
            "api.services.generation_service.get_day_part_hours",
            return_value=1,
        ), patch(
            "api.services.generation_service.mmr_select",
            return_value=[candidate],
        ), patch(
            "api.services.generation_service._sync_playlist_artifacts"
        ) as sync_artifacts, patch(
            "api.services.generation_service.log_activity"
        ) as log_activity:
            generation_service._bg_playlist(
                "brand-1",
                task_id,
                genre_overrides={"include": ["pop"], "exclude": []},
                playlist_name="Evening Set",
            )

        task = tasks.get_task(task_id)
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["progress"], 100)

        brand = store.get_brands()["brand-1"]
        self.assertEqual(brand["playlist_count"], 1)
        self.assertEqual(brand["playlist_name"], "Evening Set")
        self.assertEqual(brand["status"], "active")
        self.assertIn("brand_profile", brand)
        self.assertIn("sound_board_result", brand)

        sync_artifacts.assert_called_once()
        log_activity.assert_called_once_with("Playlists generated (MMR)", "Fab Cafe")
