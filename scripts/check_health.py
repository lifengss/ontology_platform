import urllib.request, json

try:
    r = urllib.request.urlopen('http://localhost:8000/health', timeout=10)
    data = json.load(r)
    print(json.dumps(data, indent=2, ensure_ascii=False))
except Exception as e:
    print('ERR', type(e).__name__, str(e))
