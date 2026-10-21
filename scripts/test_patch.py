import urllib.request, json

base = 'http://localhost:8000'
oid = '3e1abc47f7f648969f601cffad6fb243'

req = urllib.request.Request(
    f'{base}/api/ontologies/{oid}/objects/obj_customer',
    data=json.dumps({
        'name': '客户（已修改）',
        'attributes': [
            {'id': 'attr_name', 'name': '客户名称', 'data_type': 'string', 'required': True, 'constraints': {}},
            {'id': 'attr_industry', 'name': '所属行业', 'data_type': 'string', 'required': False, 'constraints': {}},
        ],
        'relations': [],
    }).encode(),
    headers={'Content-Type': 'application/json'},
    method='PATCH',
)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        print('status', r.status)
        print(json.load(r))
except Exception as e:
    print('ERR', type(e).__name__, str(e))
