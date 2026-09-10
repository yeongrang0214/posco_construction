"""Read-only production snapshot + local PDF QA; never updates reviewer decisions."""
import argparse
import json
from pathlib import Path

from urllib.request import urlopen

from server.source_preview import convert_pdf, map_clauses


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    pdf = convert_pdf(args.source, args.output / "steel-source.pdf")
    endpoint = f"https://posco-construction-api-production.up.railway.app/api/projects/{args.project}/document-map"
    with urlopen(endpoint, timeout=30) as response:
        clauses = json.load(response)["items"]
    mapping = map_clauses(json.loads(pdf.with_suffix(".json").read_text(encoding="utf-8")), clauses)
    (args.output / "mapping.json").write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    (args.output / "clauses.json").write_text(json.dumps(clauses, ensure_ascii=False), encoding="utf-8")
    failures = [{"order": clause["source_order"], "title": clause["title"]} for clause, item in zip(clauses, mapping["items"]) if item["status"] != "exact"]
    print(json.dumps({"pages": len(mapping["pages"]), "mapped": mapping["mapped_count"], "total": mapping["total_count"], "unmapped": failures}, ensure_ascii=False))


if __name__ == "__main__":
    main()
