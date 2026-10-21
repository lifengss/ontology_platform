import json
import requests
BASE = "http://localhost:8000"
oid = "dc984a7cf990490f9d608824b42687f8"
r = requests.get(f"{BASE}/api/ontologies/{oid}", timeout=10)
o = r.json()["data"]
print("name", o["name"], "status", o.get("status"))
for obj in o["objects"]:
    print(obj["id"], obj["name"], [a["id"] for a in obj.get("attributes", [])])
