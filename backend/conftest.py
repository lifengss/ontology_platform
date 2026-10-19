"""pytest 配置：把 backend 加入 sys.path，并将运行时目录指向临时目录，避免污染。"""
import os
import sys
import tempfile

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

_TMP = tempfile.mkdtemp(prefix="ont_test_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["SECRETS_DIR"] = os.path.join(_TMP, "secrets")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["JENA_BASE_URL"] = "http://127.0.0.1:9"  # 不可达 → 文件降级
