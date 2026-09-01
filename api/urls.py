from django.urls import path

from api.views import auth_views, brand_views, catalog_views, generation_views, iam_views, playlist_views, stats_views
from api.views.health_views import health


urlpatterns = [
    path("health", health, name="api-health"),
    path("config", auth_views.config, name="api-config"),
    path("auth/signup", auth_views.signup, name="api-auth-signup"),
    path("auth/me", auth_views.me, name="api-auth-me"),
    path("init", stats_views.init, name="api-init"),
    path("stats", stats_views.stats, name="api-stats"),
    path("activity", stats_views.activity, name="api-activity"),
    path("brands", brand_views.brands_collection, name="api-brands-collection"),
    path("brands/<str:brand_id>", brand_views.delete_brand_view, name="api-brands-delete"),
    path(
        "brands/<str:brand_id>/playlist-name",
        brand_views.rename_playlist_view,
        name="api-brands-rename-playlist",
    ),
    path(
        "brands/<str:brand_id>/soundboard",
        brand_views.update_soundboard_view,
        name="api-brands-update-soundboard",
    ),
    path(
        "brands/<str:brand_id>/dayparts",
        brand_views.update_dayparts_view,
        name="api-brands-update-dayparts",
    ),
    path(
        "brands/<str:brand_id>/profile",
        brand_views.update_profile_view,
        name="api-brands-update-profile",
    ),
    path(
        "brands/<str:brand_id>/genres",
        brand_views.update_genres_view,
        name="api-brands-update-genres",
    ),
    path("quick-analyze", generation_views.quick_analyze_view, name="api-quick-analyze"),
    path(
        "analyze-assets-preview",
        generation_views.analyze_assets_preview_view,
        name="api-analyze-assets-preview",
    ),
    path(
        "brands/<str:brand_id>/assets",
        generation_views.upload_assets_view,
        name="api-brand-assets",
    ),
    path(
        "soundboard/<str:brand_id>",
        generation_views.start_soundboard_view,
        name="api-start-soundboard",
    ),
    path(
        "generate/<str:brand_id>",
        generation_views.start_generate_view,
        name="api-start-generate",
    ),
    path(
        "generate/status/<str:task_id>",
        generation_views.generation_status_view,
        name="api-generation-status",
    ),
    path("brands/<str:brand_id>/playlists", playlist_views.list_brand_playlists_view, name="api-brand-playlists-list"),
    path("playlists/by-id/<str:playlist_id>", playlist_views.get_playlist_by_id_view, name="api-playlist-get-by-id"),
    path("playlists/<str:brand_id>", playlist_views.get_playlist_view, name="api-playlist-get"),
    path(
        "playlists/<str:brand_id>/tracks",
        playlist_views.remove_tracks_view,
        name="api-playlist-remove-tracks",
    ),
    path(
        "playlists/<str:brand_id>/tracks/replace",
        playlist_views.replace_track_view,
        name="api-playlist-replace-track",
    ),
    path(
        "playlists/<str:brand_id>/tracks/suggest",
        playlist_views.suggest_tracks_view,
        name="api-playlist-suggest-tracks",
    ),
    path(
        "playlists/<str:brand_id>/tracks/suggest/save",
        playlist_views.add_suggested_tracks_view,
        name="api-playlist-add-suggested-tracks",
    ),
    path("catalog/genres", catalog_views.genres, name="api-catalog-genres"),
    path("catalog/libraries", catalog_views.libraries, name="api-catalog-libraries"),
    path("catalog/stats", catalog_views.stats, name="api-catalog-stats"),
    path("catalog/songs", catalog_views.songs, name="api-catalog-songs"),
    path("debug/search", iam_views.debug_search, name="api-debug-search"),
    path("debug/songs", iam_views.debug_songs, name="api-debug-songs"),
    path("iam/users", iam_views.list_users, name="api-iam-users"),
    path("iam/users/<str:uid>/role", iam_views.update_role, name="api-iam-update-role"),
]
