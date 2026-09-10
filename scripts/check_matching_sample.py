"""Local smoke evaluation of actual steel clauses, without changing saved decisions."""
import json
from pathlib import Path

from server.config import get_settings
from server.matcher import match_clauses
from server.openai_ai import OpenAIClient

settings = get_settings()
clauses = json.loads(Path("tmp/preview-v52/clauses.json").read_text(encoding="utf-8"))
orders = {38, 39, 40, 42}
selected = [dict(row) for row in clauses if row["source_order"] in orders]
for term in ("공작도", "고장력볼트"):
    match = next((row for row in clauses if term in f"{row['title']} {row['content']}" and row["source_type"] == "paragraph"), None)
    if match and match["id"] not in {row["id"] for row in selected}:
        selected.append(dict(match))
client = OpenAIClient.from_settings(settings)
try:
    print(json.dumps({"phase": "start", "count": len(selected), "ai_configured": client.available}), flush=True)
    report = match_clauses(selected, settings.kcs_raw_dir, ("4131", "1431"), client)
    summary = {"matching": report, "clauses": [{"order": row["source_order"], "source": f"{row['title']} {row['content']}", "candidates": row["candidates"]} for row in selected]}
    Path("tmp/preview-v52/matching-sample.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"phase": "complete", "clauses": [{"order": row["source_order"], "count": len(row["candidates"]), "verdicts": [c["classification"] for c in row["candidates"]], "titles": [c["title"] for c in row["candidates"]]} for row in selected]}, ensure_ascii=False), flush=True)
finally:
    client.close()
