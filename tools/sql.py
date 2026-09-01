"""Run SQL against Supabase via the Management API (needs SUPABASE_TOKEN)."""
import json, os, sys, urllib.request
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
TOKEN = os.getenv("SUPABASE_TOKEN", "").strip()
REF = os.getenv("SUPABASE_PROJECT_REF", "iruimptvnjrpmgtebjzq").strip()


def run_sql(sql: str):
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{REF}/database/query",
        data=json.dumps({"query": sql}).encode(),
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "supabase-cli/2.116.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode() or "[]")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"SQL FAILED {e.code}: {e.read().decode()[:800]}")


if __name__ == "__main__":
    sql = Path(sys.argv[1]).read_text(encoding="utf-8") if Path(sys.argv[1]).exists() else sys.argv[1]
    print(json.dumps(run_sql(sql), indent=2)[:4000])
