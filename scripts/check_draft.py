import requests
BASE = "http://localhost:8000"
r = requests.get(f"{BASE}/api/ontologies", params={"page": 1, "pageSize": 5}, timeout=10)
data = r.json()
items = data["data"]["items"]
if not items:
    print("no ontologies")
else:
    oid = items[0]["id"]
    r2 = requests.get(f"{BASE}/api/ontologies/{oid}", timeout=10)
    o = r2.json()["data"]
    print("name", o["name"], "status", o.get("status"))
    for obj in o["objects"]:
        print(obj["id"], obj["name"], [a["id"] for a in obj.get("attributes", [])[:5]])
