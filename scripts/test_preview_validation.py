import urllib.request
import json

base = 'http://localhost:8000'
oid = '3e1abc47f7f648969f601cffad6fb243'


def post(path, body):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


print(post('/api/ingest/preview', {
    'ontology_id': oid,
    'source': {'kind': 'field_map', 'records': [{'industry': '互联网'}]},
    'mapping': {'object_type': 'obj_customer', 'field_map': {'industry': 'attr_industry'}},
}))

print(post('/api/ingest/preview', {
    'ontology_id': oid,
    'source': {'kind': 'field_map', 'records': [{'n': '腾讯', 'industry': 123}]},
    'mapping': {'object_type': 'obj_customer', 'field_map': {'n': 'attr_name', 'industry': 'attr_industry'}},
}))

print(post('/api/ingest/preview', {
    'ontology_id': oid,
    'source': {'kind': 'field_map', 'records': [{'n': '腾讯'}]},
    'mapping': {'object_type': 'obj_customer', 'field_map': {'n': 'attr_name'}},
}))
