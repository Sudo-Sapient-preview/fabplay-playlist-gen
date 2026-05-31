import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import Client, SimpleTestCase, override_settings

import api.store as store


class ApiRegressionTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client(HTTP_HOST="localhost")
        self.user = {"id": "user-123", "email": "test@example.com"}
        self.headers = {"HTTP_AUTHORIZATION": "Bearer fake-token"}

        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.songs_dir = self.tmp_path / "songs"
        self.songs_dir.mkdir()
        (self.songs_dir / "demo.mp3").write_bytes(b"FAKEAUDIO")

        self._old_brands_file = store.BRANDS_FILE
        store.BRANDS_FILE = self.tmp_path / "brands.json"
        store.save_brands({})

    def tearDown(self):
        store.BRANDS_FILE = self._old_brands_file
        self._tmpdir.cleanup()
        super().tearDown()

    def _auth_patches(self, role="admin"):
        return (
            patch("api.auth.validate_token", return_value=(self.user, None, None)),
            patch("api.auth.get_user_role", return_value=role),
            patch("api.views.auth_views.get_user_role", return_value=role),
            patch("api.views.stats_views.get_user_role", return_value=role),
        )

    def test_public_health_and_config(self):
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["phase"], 7)

        config = self.client.get("/api/config")
        self.assertEqual(config.status_code, 200)
        self.assertIn("supabase_url", config.json())
        self.assertIn("supabase_anon_key", config.json())

    def test_brand_bootstrap_flow(self):
        with self._auth_patches()[0], self._auth_patches()[1], self._auth_patches()[2], self._auth_patches()[3]:
            me = self.client.get("/api/auth/me", **self.headers)
            self.assertEqual(me.status_code, 200)
            self.assertEqual(me.json()["email"], self.user["email"])

            create = self.client.post(
                "/api/brands",
                data=json.dumps({"brand_name": "Fab Cafe", "category": "cafe"}),
                content_type="application/json",
                **self.headers,
            )
            self.assertEqual(create.status_code, 200)
            brand_id = create.json()["id"]

            brands = self.client.get("/api/brands", **self.headers)
            self.assertEqual(brands.status_code, 200)
            self.assertEqual(len(brands.json()), 1)

            init = self.client.get("/api/init", **self.headers)
            self.assertEqual(init.status_code, 200)
            self.assertEqual(len(init.json()["brands"]), 1)

            rename = self.client.patch(
                f"/api/brands/{brand_id}/playlist-name",
                data=json.dumps({"name": "Evening Set"}),
                content_type="application/json",
                **self.headers,
            )
            self.assertEqual(rename.status_code, 200)
            self.assertTrue(rename.json()["ok"])

    def test_generation_endpoint_contracts(self):
        store.save_brands(
            {
                "brand-1": {
                    "id": "brand-1",
                    "user_id": self.user["id"],
                    "brand_name": "Fab Cafe",
                    "category": "cafe",
                    "playlist_count": 0,
                    "status": "setup",
                }
            }
        )

        auth_patches = self._auth_patches()
        with auth_patches[0], auth_patches[1], auth_patches[2], auth_patches[3], patch(
            "api.views.generation_views.quick_analyze",
            return_value={"customer_segment": "premium"},
        ), patch(
            "api.views.generation_views.analyze_files",
            return_value={
                "asset_analysis": "analysis",
                "recommended_genres": ["pop"],
                "avoid_genres": ["metal"],
                "music_notes": "notes",
                "has_brand_guidelines": True,
            },
        ), patch(
            "api.views.generation_views.start_soundboard",
            return_value=("task-sb", None, None),
        ), patch(
            "api.views.generation_views.start_playlist_generation",
            return_value=("task-pl", None, None),
        ), patch(
            "api.views.generation_views.get_generation_task",
            return_value=({"status": "pending", "progress": 42, "log": ["ok"], "error": None}, None, None),
        ):
            quick = self.client.post(
                "/api/quick-analyze",
                data=json.dumps({"brand_name": "Fab", "category": "cafe"}),
                content_type="application/json",
                **self.headers,
            )
            self.assertEqual(quick.status_code, 200)
            self.assertEqual(quick.json()["customer_segment"], "premium")

            upload = self.client.post(
                "/api/analyze-assets-preview",
                data={"files": [self._pdf_upload()]},
                **self.headers,
            )
            self.assertEqual(upload.status_code, 200)
            self.assertIn("asset_analysis", upload.json())

            brand_upload = self.client.post(
                "/api/brands/brand-1/assets",
                data={"files": [self._pdf_upload()]},
                **self.headers,
            )
            self.assertEqual(brand_upload.status_code, 200)
            self.assertTrue(brand_upload.json()["has_brand_guidelines"])

            soundboard = self.client.post("/api/soundboard/brand-1", **self.headers)
            self.assertEqual(soundboard.status_code, 200)
            self.assertEqual(soundboard.json()["task_id"], "task-sb")

            generate = self.client.post(
                "/api/generate/brand-1",
                data=json.dumps({"genre_overrides": {"include": ["pop"]}, "playlist_name": "Evening"}),
                content_type="application/json",
                **self.headers,
            )
            self.assertEqual(generate.status_code, 200)
            self.assertEqual(generate.json()["task_id"], "task-pl")

            status = self.client.get("/api/generate/status/task-pl", **self.headers)
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json()["progress"], 42)

    def test_playlist_catalog_iam_and_songs_contracts(self):
        store.save_brands(
            {
                "brand-1": {
                    "id": "brand-1",
                    "user_id": self.user["id"],
                    "brand_name": "Fab Cafe",
                    "category": "cafe",
                    "playlist_count": 1,
                    "status": "active",
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
                            "title": "Track 1",
                            "src": "songs/demo.mp3",
                            "url": "songs/demo.mp3",
                            "duration_seconds": 180,
                            "bfs": 0.7,
                            "mmr_score": 0.6,
                        }
                    ],
                }
            ]
        }

        auth_patches = self._auth_patches(role="superadmin")
        mock_supabase = MagicMock()
        mock_supabase.table.return_value.select.return_value.execute.return_value.data = [
            {"user_id": self.user["id"], "role": "superadmin"}
        ]
        mock_supabase.table.return_value.select.return_value.execute.return_value.count = 10
        mock_supabase.table.return_value.select.return_value.limit.return_value.execute.return_value.data = [
            {"id": "song-1", "title": "Track 1", "artist": "Artist", "url": "songs/demo.mp3"}
        ]
        mock_supabase.rpc.return_value.execute.return_value.data = [{"id": "song-1"}]
        mock_catalog_supabase = MagicMock()
        mock_catalog_supabase.table.return_value.select.return_value.execute.return_value.count = 10
        mock_catalog_supabase.table.return_value.select.return_value.limit.return_value.execute.return_value.data = [
            {
                "id": "song-1",
                "title": "Track 1",
                "artist": "Artist",
                "genre": "pop",
                "url": "songs/demo.mp3",
                "tempo_bpm": 100,
                "energy": 0.5,
                "valence": 0.5,
                "danceability": 0.5,
                "acousticness": 0.4,
                "instrumentalness": 0.3,
                "loudness": -8.0,
                "speechness": 0.1,
                "duration_seconds": 180,
            }
        ]

        with auth_patches[0], auth_patches[1], auth_patches[2], auth_patches[3], patch(
            "api.services.playlist_service._fetch_playlist_from_supabase",
            return_value=playlist_data,
        ), patch(
            "api.views.playlist_views.remove_tracks",
            return_value=({"ok": True, "day_part": {"tracks": []}}, None, None),
        ), patch(
            "api.views.playlist_views.replace_track",
            return_value=({"ok": True, "replacement": {"song_id": "song-2"}, "day_part": {"tracks": [{"song_id": "song-2"}]}}, None, None),
        ), patch(
            "api.services.catalog_service.fetch_all_songs",
            return_value=[{"genre": "pop"}, {"genre": "jazz"}],
        ), patch(
            "api.services.catalog_service.get_supabase",
            return_value=mock_catalog_supabase,
        ), patch(
            "api.views.iam_views.get_supabase",
            return_value=mock_supabase,
        ), patch(
            "api.views.iam_views.httpx.Client"
        ) as mock_httpx, override_settings(
            SONGS_BASE_URL="",
            SONGS_DIR=str(self.songs_dir),
        ):
            mock_httpx.return_value.__enter__.return_value.get.return_value.status_code = 200
            mock_httpx.return_value.__enter__.return_value.get.return_value.json.return_value = {
                "users": [
                    {
                        "id": self.user["id"],
                        "email": self.user["email"],
                        "created_at": "now",
                        "last_sign_in_at": "later",
                    }
                ]
            }

            playlist = self.client.get("/api/playlists/brand-1", **self.headers)
            self.assertEqual(playlist.status_code, 200)
            self.assertEqual(playlist.json()["day_parts"][0]["tracks"][0]["src"], "/songs/demo.mp3")

            removed = self.client.delete(
                "/api/playlists/brand-1/tracks",
                data=json.dumps({"day_part_index": 0, "song_ids": ["song-1"]}),
                content_type="application/json",
                **self.headers,
            )
            self.assertEqual(removed.status_code, 200)

            replaced = self.client.post(
                "/api/playlists/brand-1/tracks/replace",
                data=json.dumps({"day_part_index": 0, "song_id": "song-1"}),
                content_type="application/json",
                **self.headers,
            )
            self.assertEqual(replaced.status_code, 200)

            self.assertEqual(self.client.get("/api/catalog/genres", **self.headers).status_code, 200)
            self.assertEqual(self.client.get("/api/catalog/stats", **self.headers).status_code, 200)
            self.assertEqual(self.client.get("/api/catalog/songs", **self.headers).status_code, 200)
            self.assertEqual(self.client.get("/api/iam/users", **self.headers).status_code, 200)
            self.assertEqual(
                self.client.put(
                    f"/api/iam/users/{self.user['id']}/role",
                    data=json.dumps({"role": "admin"}),
                    content_type="application/json",
                    **self.headers,
                ).status_code,
                200,
            )
            self.assertEqual(self.client.get("/api/debug/search", **self.headers).status_code, 200)
            self.assertEqual(self.client.get("/api/debug/songs", **self.headers).status_code, 200)

            song_response = self.client.get("/songs/demo.mp3")
            song_bytes = b"".join(song_response.streaming_content)
            song_response.close()
            self.assertEqual(song_response.status_code, 200)
            self.assertEqual(song_bytes, b"FAKEAUDIO")

    def _pdf_upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile(
            "brand-guide.pdf",
            b"%PDF-1.4 fake",
            content_type="application/pdf",
        )
