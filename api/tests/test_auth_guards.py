import json
from unittest.mock import MagicMock, patch

from django.http import JsonResponse
from django.test import RequestFactory, SimpleTestCase

import api.auth as auth


class AuthGuardTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        auth._auth_cache.clear()
        auth._role_cache.clear()

    def tearDown(self):
        auth._auth_cache.clear()
        auth._role_cache.clear()
        super().tearDown()

    def test_validate_token_uses_cache(self):
        mock_client = MagicMock()
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"id": "user-1", "email": "user@example.com"}
        mock_client.get.return_value = mock_response

        with patch.object(auth, "SUPABASE_URL", "https://example.supabase.co"), patch.object(
            auth,
            "SUPABASE_SERVICE_KEY",
            "service-key",
        ), patch.object(auth, "_get_http_client", return_value=mock_client):
            first_user, first_status, first_message = auth.validate_token("token-123")
            second_user, second_status, second_message = auth.validate_token("token-123")

        self.assertEqual(first_status, None)
        self.assertEqual(first_message, None)
        self.assertEqual(second_status, None)
        self.assertEqual(second_message, None)
        self.assertEqual(first_user["id"], "user-1")
        self.assertEqual(second_user["id"], "user-1")
        self.assertEqual(mock_client.get.call_count, 1)

    def test_require_superadmin_rejects_non_superadmin(self):
        @auth.require_superadmin
        def protected(_request):
            return JsonResponse({"ok": True})

        request = self.factory.get("/api/iam/users")

        with patch.object(
            auth,
            "authenticate_request",
            return_value=({"id": "user-1"}, "token-123", None, None),
        ), patch.object(auth, "get_user_role", return_value="viewer"):
            response = protected(request)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.content)["error"], "Superadmin access required")

    def test_require_superadmin_sets_request_role(self):
        @auth.require_superadmin
        def protected(request):
            return JsonResponse({"ok": True, "role": request.user_role})

        request = self.factory.get("/api/iam/users")

        with patch.object(
            auth,
            "authenticate_request",
            return_value=({"id": "user-1"}, "token-123", None, None),
        ), patch.object(auth, "get_user_role", return_value="superadmin"):
            response = protected(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["role"], "superadmin")
