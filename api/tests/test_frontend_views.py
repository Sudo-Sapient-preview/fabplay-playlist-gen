from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from webapp.views import songs_proxy


class FrontendViewTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()

    @override_settings(SONGS_BASE_URL="https://songs.example.com")
    def test_songs_proxy_forwards_range_headers(self):
        request = self.factory.get("/songs/demo.mp3", HTTP_RANGE="bytes=0-99")
        upstream = MagicMock()
        upstream.status_code = 206
        upstream.content = b"FAKEAUDIO"
        upstream.headers = {
            "content-type": "audio/mpeg",
            "content-range": "bytes 0-99/1000",
            "accept-ranges": "bytes",
            "content-length": "100",
        }

        with patch("webapp.views.httpx.Client") as httpx_client:
            httpx_client.return_value.__enter__.return_value.get.return_value = upstream
            response = songs_proxy(request, "demo.mp3")

        httpx_client.return_value.__enter__.return_value.get.assert_called_once_with(
            "https://songs.example.com/demo.mp3",
            headers={"Range": "bytes=0-99"},
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"FAKEAUDIO")
        self.assertEqual(response["Content-Range"], "bytes 0-99/1000")
        self.assertEqual(response["Accept-Ranges"], "bytes")
        self.assertEqual(response["Content-Length"], "100")
