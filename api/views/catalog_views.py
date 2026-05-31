from django.views.decorators.http import require_GET

from api.auth import require_auth
from api.services.catalog_service import get_artists, get_catalog_stats, get_genres, get_sample_songs
from api.utils import ok


@require_GET
@require_auth
def genres(_request):
    return ok(get_genres())


@require_GET
@require_auth
def artists(_request):
    return ok(get_artists())


@require_GET
@require_auth
def stats(_request):
    return ok(get_catalog_stats())


@require_GET
@require_auth
def songs(_request):
    return ok(get_sample_songs())
