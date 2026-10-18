import csv, json, sys
from pathlib import Path
RUNS = {"0": "run-784c6db7137a", "A": "run-8b485bb81808", "B": "run-64ef248eda22", "C": "run-54d6dbfbdea3"}
EXPECTED = [
    "[FACT] Mechanism: the left MLG aft trunnion pin failed by fatigue fracture, with final overstress fracture during landing",
    "[FACT] Crack origin: initiated at a small intergranular region below the chromium plating",
    "[FACT] Heat evidence: temper etch and Barkhausen noise readings consistent with the region having been exposed to higher temperatures",
    "[INFERENCE] Root cause: excessive grinding/machining during the July 2018 overhaul imparted heat damage that led to the fatigue cracking",
    "[FACT] Timeline: crack not present at overhaul; present for at least 797 landing cycles (4,710 cycles since overhaul)",
]
out = Path("data/reports"); out.mkdir(parents=True, exist_ok=True)
col = "agreement (match/partial/miss)"
if "--score" in sys.argv:
    pts = {"match": 1, "partial": 0.5, "miss": 0}; tot = {}
    for r in csv.DictReader(open(out / "manual_review.csv", encoding="utf-8-sig")):
        a = r[col].strip().lower()
        if a in pts:
            tag = "INFERENCE" if r["expected_finding"].startswith("[INFERENCE]") else "FACT"
            t = tot.setdefault((r["run"], tag), [0, 0]); t[0] += pts[a]; t[1] += 1
    for (run, tag), (s, n) in sorted(tot.items()): print(f"Run {run} {tag}: {s}/{n}")
    sys.exit()
print(f"{'run':4}{'in_tok':>8}{'latency_s':>10}{'checks':>7}{'flagged':>8}  status")
rows = []
for label, rid in RUNS.items():
    r = json.loads(Path(f"data/runs/{rid}.json").read_text(encoding="utf-8"))
    m, v = r["metrics"], (r.get("verification") or {})
    ch = v.get("claim_checks", []); fl = sum(c["downgraded"] for c in ch)
    print(f"{label:4}{m['input_tokens']:>8}{m['latency_s']:>10.1f}{len(ch):>7}{fl:>8}  {v.get('status', 'plain text')}")
    summ = (r["assessment"] or {}).get("brief_summary") or (r["raw_output"] or "")[:300]
    rows += [{"run": label, "run_id": rid, "expected_finding": e, "model_summary": summ, col: "", "notes": ""} for e in EXPECTED]
with open(out / "manual_review.csv", "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
print("Fill the agreement column in data/reports/manual_review.csv, then run: py eval_report.py --score")
