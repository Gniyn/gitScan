import json, sys, time, urllib.error, urllib.request
BASE = "http://127.0.0.1:8000"

def post(path, data):
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=200).read().decode())

def get(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=200).read().decode())

sid = post("/api/scans", {"url": sys.argv[1]})["id"]
for _ in range(160):
    d = get("/api/scans/" + sid)
    if d["status"] in ("complete", "partial", "failed"):
        break
    time.sleep(1.5)
print("status:", d["status"])
print("error:", d.get("error"))
r = d.get("report")
if r:
    print("score:", r["score"], "commit:", r["commit_sha"][:10])
    for k, v in r["dimensions"].items():
        print("  ", k, v["score"], "/", v["max"])
    print("findings:", len(r["findings"]))
    if len(sys.argv) > 2:
        with open(sys.argv[2], "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
        print("saved:", sys.argv[2])
