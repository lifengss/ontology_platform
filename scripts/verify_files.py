import os

files = {
    'backend_ingestion': r'd:\self_coding\ontology_platform\backend\ingestion\__init__.py',
    'frontend_app': r'd:\self_coding\ontology_platform\frontend\app.js',
}

report = []
for name, path in files.items():
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    lines = content.splitlines()
    report.append(f'=== {name}: {path} ===')
    report.append(f'chars={len(content)} lines={len(lines)}')
    report.append(f'first_line={lines[0]!r}')
    report.append(f'contains_python_def={content.strip().startswith("\"\"\"ingestion")}')
    report.append(f'contains_js_function={"function" in content}')
    report.append(f'contains_preview_validate={"_validate_record_against_object" in content}')
    report.append(f'contains_tabular_review={"renderAttrInput" in content}')
    report.append('')

out_path = r'd:\self_coding\ontology_platform\scripts\verify_report.txt'
with open(out_path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(report))
print('report written to', out_path)
