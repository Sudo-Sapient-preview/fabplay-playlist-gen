import json
import threading
from pathlib import Path
from typing import Any


DATA_DIR = Path("data")
BRANDS_FILE = DATA_DIR / "brands_store.json"

_store_lock = threading.RLock()


def load_json(path: Path, default: Any):
    with _store_lock:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default


def save_json(path: Path, data: Any) -> None:
    with _store_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def get_brands() -> dict:
    return load_json(BRANDS_FILE, {})


def save_brands(brands: dict) -> None:
    save_json(BRANDS_FILE, brands)


def get_brand(brand_id: str) -> dict | None:
    return get_brands().get(brand_id)


def upsert_brand(brand: dict) -> dict:
    brand_id = str(brand.get("id") or "")
    if not brand_id:
        raise ValueError("Brand must include a non-empty id")
    brands = get_brands()
    brands[brand_id] = brand
    save_brands(brands)
    return brand


def delete_brand(brand_id: str) -> bool:
    brands = get_brands()
    if brand_id not in brands:
        return False
    del brands[brand_id]
    save_brands(brands)
    return True
