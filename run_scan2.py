import json, sys, time, urllib.request
BASE = "http://127.0.0.1:8000"
def post(p, d):
    req = urllib.request.Request(BASE + p, data=json.dumps(d).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=200).read().decode())
def get(p):
    return json.loads(urllib.request.urlopen(BASE + p, timeout=200).read().decode())

sid = post("/api/scans", {"url": sys.argv[1]})["id"]
for _ in range(200):
    d = get("/api/scans/" + sid)
    if d["status"] in ("complete", "partial", "failed"):
        break
    time.sleep(1.5)
print("status:", d["status"], "| error:", d.get("error"))
r = d.get("report")
if r:
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=2)
    print("score:", r["score"], "commit:", r["commit_sha"][:10], "findings:", len(r["findings"]))
    print("dims:", {k: v["score"] for k, v in r["dimensions"].items()})
    for f in r["findings"]:
        if f.get("applied_deduction"):
            print("  -", f["rule_id"], f.get("path"), f.get("hit_count"), f["applied_deduction"])
