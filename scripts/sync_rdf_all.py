"""RDF 全量回填：把所有项目下所有本体的全量图（schema + 实例）重建并写入 RDF 存储。

- Fuseki 可达 → 每个本体写入独立命名图 urn:ontology:{id}（http://localhost:3030/ontology）
- 不可达     → 降级写本地 data/projects/<pid>/ontology_<oid>.ttl

用法:
    python scripts/sync_rdf_all.py [--project <project_id>]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from jena_adapter import rdf_sync  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="RDF 全量图回填")
    parser.add_argument("--project", default=None, help="仅回填指定项目（默认全部项目）")
    args = parser.parse_args()

    results = rdf_sync.sync_all_projects(project_id=args.project)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    failed = [r for r in results if r.get("error")]
    ok = len(results) - len(failed)
    print(f"\n回填完成: 成功 {ok}/{len(results)}，失败 {len(failed)}", file=sys.stderr)
    if failed:
        for r in failed:
            print(f"  失败: project={r['project_id']} ontology={r.get('name')} ({r.get('error')})", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
