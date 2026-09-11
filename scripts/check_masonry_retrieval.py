"""Offline regression recall; related evidence is NOT a full-deletion gold label."""
import argparse
import json
import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.matcher import _rank_matches, _rank_without_embeddings, _candidate_rows

# Verified source concerns and KCS body anchors from the 2026-09-11 audit.
# The standard corpus itself is loaded from the current local KCSC snapshot.
CASES = [
    ("joint", "콘크리트 벽돌쌓기의 줄눈은 통줄눈을 피하고 가로, 세로 10㎜ 로 한다.", "콘크리트 벽돌", "413402", "통줄눈"),
    ("ties", "공간벽돌 쌓기 시 연결재는 수평거리 90㎝, 수직거리 50㎝ 이내로 한다.", "콘크리트 벽돌", "413402", "900"),
    ("daily", "1일쌓기는 1.5m(7켜) 이내로 하고 하루일 끝마감부분의 처리에 대하여 검사한다.", "콘크리트 블록", "413406", "7켜"),
    ("joint_finish", "쌓은 후는 줄눈몰탈을 눌러두고 줄파기가 적당한가를 확인한다.", "콘크리트 블록", "413406", "줄눈파기"),
    ("lime", "미장용 소석회는 규정에 합격한 것으로 한다.", "콘크리트 벽돌", "413402", "소석회"),
    ("clay", "점토벽돌은 KS L 4201에 합격한 것으로 하며 압축강도 10.78N/mm² 이상 흡수율 15% 이하로 한다.", "점토 벽돌", "413402", "한국산업표준"),
    ("day_clay", "점토벽돌은 1일 1m 이내로 쌓고 매켜마다 수평실을 치며 줄눈에 모르타르를 채운다.", "점토 벽돌", "413402", "하루의 쌓기 높이"),
    ("level", "수직 수평을 보아 쌓기면의 요철이 없도록 한다.", "콘크리트 블록", "413406", "수평"),
    ("mortar", "몰탈이 충분히 충진 되도록 쌓는다.", "콘크리트 블록", "413406", "모르타르"),
    ("reinforcement", "세로근은 기초, 테두리보에 정착하고 보강근이 있는 빈속은 콘크리트를 사춤한다.", "보강 블록", "413407", "정착"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("raw", type=Path)
    parser.add_argument("--baseline-ref", help="Read an old matcher with git show, without modifying the checkout")
    args = parser.parse_args()
    rank_matches, rank_without_embeddings, candidate_rows = _rank_matches, _rank_without_embeddings, _candidate_rows
    if args.baseline_ref:
        source = subprocess.run(["git", "show", args.baseline_ref + ":server/matcher.py"], check=True,
                                capture_output=True, encoding="utf-8").stdout
        namespace = {"__name__": "server.matcher_baseline", "__package__": "server"}
        exec(compile(source, "matcher_baseline", "exec"), namespace)
        rank_matches, rank_without_embeddings, candidate_rows = [namespace[n] for n in ("_rank_matches", "_rank_without_embeddings", "_candidate_rows")]
    results = []
    for name, text, context, code, anchor in CASES:
        clause = {"id": "00000000-0000-0000-0000-000000000001", "title": "", "content": text,
                  "match_context": context, "source_type": "paragraph"}
        ranked = rank_matches(text + " " + context, args.raw, ("4134",))
        def hit(section):
            return section["code"].replace("KCS", "").replace(" ", "") == code and anchor.replace(" ", "") in section["content"].replace(" ", "")
        rank = next((i for i, (s, _) in enumerate(ranked, 1) if hit(s)), None)
        candidates = candidate_rows(clause, text, rank_without_embeddings(ranked), limit=12, min_relevance=.1)
        passed = any(c["kcs_code"].replace("KCS", "").replace(" ", "") == code and anchor.replace(" ", "") in c["content"].replace(" ", "") for c in candidates)
        results.append({"case": name, "pool_rank": rank, "verifier_pool_found": passed,
                        "top": [{"code": c["kcs_code"], "title": c["title"], "clause": c["kcs_clause"]} for c in candidates[:3]]})
    print(json.dumps({"pool_recall": sum(r["pool_rank"] is not None for r in results),
                      "verifier_pool_recall": sum(r["verifier_pool_found"] for r in results),
                      "total": len(results), "cases": results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
