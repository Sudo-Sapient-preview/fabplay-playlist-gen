from api.utils import ok


def health(_request):
    return ok({"ok": True, "service": "api", "phase": 7})
