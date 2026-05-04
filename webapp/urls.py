from django.urls import path, re_path

from webapp import views


urlpatterns = [
    path("", views.index, name="index"),
    path("login", views.login_page, name="login"),
    path("auth/callback", views.auth_callback, name="auth-callback"),
    path("dashboard", views.dashboard, name="dashboard"),
    re_path(r"^dashboard/(?P<path>.*)$", views.dashboard, name="dashboard-path"),
    path("favicon.ico", views.favicon, name="favicon"),
    path("songs/<path:path>", views.songs_proxy, name="songs-proxy"),
]
