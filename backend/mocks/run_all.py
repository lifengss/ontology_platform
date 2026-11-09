"""独立运行三个可控 mock 外部系统（VS2-00）。

用法：
    python -m backend.mocks.run_all [--base-port 18101] [--seed 42]

端口：crm=<base>, plm=<base+1>, reservoir=<base+2>。
管理面：X-Mock-Admin: mock-admin-2026
数据面：Authorization: Bearer tok-full | tok-even | tok-odd | tok-empty
"""

import argparse
import time

from .v2_external import make_all

PORTS = {"crm": 0, "plm": 1, "reservoir": 2}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-port", type=int, default=18101)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    systems = make_all(seed=args.seed)
    for name, offset in PORTS.items():
        port = args.base_port + offset
        systems[name].start(port=port)
        print(f"[mock] {name} -> http://127.0.0.1:{port}  (channel default: "
              f"{systems[name].config['cdc_channel']})")
    print("[mock] 全部就绪；Ctrl+C 退出。管理面需 X-Mock-Admin: mock-admin-2026")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        for s in systems.values():
            s.stop()


if __name__ == "__main__":
    main()
