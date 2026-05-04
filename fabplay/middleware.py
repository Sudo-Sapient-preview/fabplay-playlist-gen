from django.utils.deprecation import MiddlewareMixin


class ApiCsrfBypassMiddleware(MiddlewareMixin):
    """
    Keep Django's standard CSRF middleware enabled while exempting the
    bearer-token API surface, which does not use session-backed CSRF tokens.
    """

    def process_request(self, request):
        if request.path.startswith("/api/"):
            request._dont_enforce_csrf_checks = True
