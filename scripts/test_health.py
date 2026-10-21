import urllib.request, json
try:
    r = urllib.request.urlopen('http://localhost:8000/health', timeout=10)
    print('status', r.status)
    print(json.load(r))
except Exception as e:
    print('ERR', type(e).__name__, str(e))
