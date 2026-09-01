from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

import api.store as store
from api.services import playlist_service


class PlaylistServiceTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        import tempfile
        from pathlib import Path
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self._old_brands_file = store.BRANDS_FILE
        store.BRANDS_FILE = self.tmp_path / "brands.json"
        store.save_brands({})

    def tearDown(self):
        store.BRANDS_FILE = self._old_brands_file
        self._tmpdir.cleanup()
        super().tearDown()

    @override_settings(SONGS_BASE_URL="https://cdn.test/songs")
    def test_get_playlist_falls_back_to_supabase_and_resolves_src(self):
        response = MagicMock()
        response.data = [
            {
                "playlist_json": {
                    "day_parts": [
                        {
                            "name": "Morning",
                            "tracks": [{"song_id": "song-1", "url": "demo.mp3"}],
                        }
                    ]
                }
            }
        ]
        mock_supabase = MagicMock()
        (
            mock_supabase.table.return_value.select.return_value.eq.return_value
            .order.return_value.limit.return_value.execute.return_value
        ) = response

        with patch("api.services.playlist_service.get_supabase", return_value=mock_supabase):
            playlist, message, status = playlist_service.get_playlist("brand-1")

        self.assertEqual((message, status), (None, None))
        self.assertEqual(
            playlist["day_parts"][0]["tracks"][0]["src"],
            "https://cdn.test/songs/demo.mp3",
        )

    def test_remove_tracks_rejects_invalid_daypart_index(self):
        store.save_brands({"brand-1": {"id": "brand-1", "user_id": "user-1"}})

        playlist_data = {"day_parts": [{"tracks": []}]}
        with patch("api.services.playlist_service._fetch_playlist_from_supabase", return_value=playlist_data):
            result, message, status = playlist_service.remove_tracks("brand-1", "user-1", 3, ["song-1"])

        self.assertEqual(result, None)
        self.assertEqual((message, status), ("Invalid day_part_index", 400))

    @override_settings(SONGS_BASE_URL="https://cdn.test/songs")
    def test_replace_track_selects_best_candidate_and_persists(self):
        store.save_brands(
            {
                "brand-1": {
                    "id": "brand-1",
                    "user_id": "user-1",
                    "brand_name": "Fab Cafe",
                    "category": "cafe",
                }
            }
        )
        playlist_data = {
            "day_parts": [
                {
                    "name": "Morning",
                    "tracks": [
                        {
                            "song_id": "song-1",
                            "title": "Original",
                            "url": "original.mp3",
                            "duration_seconds": 180,
                            "mmr_score": 0.3,
                            "bfs": 0.4,
                        }
                    ],
                }
            ]
        }
        candidates = [
            {
                "id": "song-2",
                "title": "Replacement Winner",
                "library": "Epic",
                "genre": "pop",
                "url": "replacement.mp3",
                "tempo_bpm": 115,
                "duration_seconds": 200,
                "energy": 0.7,
                "valence": 0.6,
                "danceability": 0.5,
                "acousticness": 0.2,
                "instrumentalness": 0.1,
                "loudness": -7.0,
                "speechness": 0.1,
                "mmr_score": 0.8,
            },
            {
                "id": "song-3",
                "title": "Replacement Loser",
                "library": "Amurco",
                "genre": "jazz",
                "url": "other.mp3",
                "tempo_bpm": 100,
                "duration_seconds": 190,
                "energy": 0.4,
                "valence": 0.4,
                "danceability": 0.3,
                "acousticness": 0.5,
                "instrumentalness": 0.2,
                "loudness": -9.0,
                "speechness": 0.1,
                "mmr_score": 0.4,
            },
        ]
        analysis = {
            "song-1": {"clap_audio_512": [9, 9], "mood_predicted_labels": ["warm"], "arousal": 0.4},
            "song-2": {"clap_audio_512": [1, 1], "mood_predicted_labels": ["bright"], "arousal": 0.8},
            "song-3": {"clap_audio_512": [0, 0], "mood_predicted_labels": ["calm"], "arousal": 0.2},
        }

        def attach_features(tracks, feature_map):
            for track in tracks:
                track.update(feature_map.get(track.get("id") or track.get("song_id"), {}))

        def compute_maest_sim(candidate, original):
            if original.get("clap_audio_512") == [9, 9] and (candidate.get("id") or candidate.get("song_id")) == "song-2":
                return 1.0
            return 0.0

        with patch("api.services.playlist_service._fetch_playlist_from_supabase", return_value=playlist_data), patch(
            "api.services.playlist_service.fetch_all_songs", return_value=candidates
        ), patch(
            "api.services.playlist_service.apply_hard_filters",
            return_value=candidates,
        ), patch(
            "api.services.playlist_service._fetch_analysis_features",
            return_value=analysis,
        ), patch(
            "api.services.playlist_service._attach_analysis_features",
            side_effect=attach_features,
        ), patch(
            "api.services.playlist_service.compute_relevance",
            return_value=0.5,
        ), patch(
            "api.services.playlist_service.compute_maest_sim",
            side_effect=compute_maest_sim,
        ), patch(
            "api.services.playlist_service._sync_playlist_songs"
        ) as sync_songs, patch(
            "api.services.playlist_service._persist_playlist_snapshot"
        ) as persist_snapshot:
            result, message, status = playlist_service.replace_track("brand-1", "user-1", 0, "song-1")

        self.assertEqual((message, status), (None, None))
        self.assertTrue(result["ok"])
        self.assertEqual(result["replacement"]["song_id"], "song-2")
        self.assertEqual(
            result["day_part"]["tracks"][0]["src"],
            "https://cdn.test/songs/replacement.mp3",
        )
        sync_songs.assert_called_once()
        persist_snapshot.assert_called_once()

    @override_settings(SONGS_BASE_URL="https://cdn.test/songs")
    def test_suggest_tracks_preranks_with_clap_then_maest(self):
        store.save_brands(
            {
                "brand-1": {
                    "id": "brand-1",
                    "user_id": "user-1",
                    "brand_name": "Fab Cafe",
                    "category": "cafe",
                }
            }
        )
        playlist_data = {
            "day_parts": [
                {
                    "name": "Morning",
                    "tracks": [{"song_id": "song-1", "title": "Seed"}],
                }
            ]
        }
        catalog = [
            {
                "id": "song-1",
                "song_id": "song-1",
                "title": "Seed",
                "library": "Epic",
                "genre": "classical",
                "url": "seed.mp3",
                "tempo_bpm": 120,
                "duration_seconds": 180,
                "energy": 0.4,
                "valence": 0.3,
                "danceability": 0.2,
                "acousticness": 0.8,
                "instrumentalness": 0.9,
                "loudness": -12.0,
                "speechness": 0.05,
            },
            {
                "id": "song-2",
                "song_id": "song-2",
                "title": "True Neighbor",
                "library": "Epic",
                "genre": "classical",
                "url": "neighbor.mp3",
                # Intentionally far on scalar features so feature-only ranking would miss it.
                "tempo_bpm": 90,
                "duration_seconds": 190,
                "energy": 0.9,
                "valence": 0.8,
                "danceability": 0.7,
                "acousticness": 0.1,
                "instrumentalness": 0.2,
                "loudness": -5.0,
                "speechness": 0.1,
            },
            {
                "id": "song-3",
                "song_id": "song-3",
                "title": "Feature Twin",
                "library": "Amurco",
                "genre": "jazz",
                "url": "twin.mp3",
                # Near-identical scalar features to the seed song.
                "tempo_bpm": 120,
                "duration_seconds": 185,
                "energy": 0.41,
                "valence": 0.31,
                "danceability": 0.21,
                "acousticness": 0.79,
                "instrumentalness": 0.88,
                "loudness": -11.5,
                "speechness": 0.05,
            },
        ]
        analysis = {
            "song-1": {"maest_audio_768": [1.0, 0.0], "clap_audio_512": [1.0, 0.0]},
            "song-2": {"maest_audio_768": [0.98, 0.02], "clap_audio_512": [0.99, 0.01]},
            "song-3": {"maest_audio_768": [0.1, 0.9], "clap_audio_512": [0.0, 1.0]},
        }

        def attach_features(tracks, feature_map):
            for track in tracks:
                track.update(feature_map.get(str(track.get("song_id") or track.get("id")), {}))

        with patch("api.services.playlist_service._fetch_playlist_from_supabase", return_value=playlist_data), patch(
            "api.services.playlist_service.fetch_all_songs", return_value=catalog
        ), patch(
            "api.services.playlist_service.apply_exclusion_filters_only",
            return_value=[dict(song) for song in catalog if song["song_id"] != "song-1"],
        ), patch(
            "api.services.playlist_service._fetch_analysis_features",
            return_value=analysis,
        ), patch(
            "api.services.playlist_service._attach_analysis_features",
            side_effect=attach_features,
        ):
            result, message, status = playlist_service.suggest_tracks(
                "brand-1", "user-1", 0, "song-1", top_k=1
            )

        self.assertEqual((message, status), (None, None))
        self.assertEqual(result["suggestions"][0]["song_id"], "song-2")
        self.assertTrue(result["suggestions"][0]["suggested"])
