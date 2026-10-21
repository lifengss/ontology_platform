import os
import sys

import pandas as pd

base = r"D:/inspur/智能化转型部/运营类/黑客松/赛题池/L6采购计划流程本体建模/赛题一-采购计划制定流程赛题数据包"
path = os.path.join(base, "01_样例数据/输出样例与模板/B0_参考本体_采购计划.xlsx")
print("exists", os.path.exists(path))
if not os.path.exists(path):
    sys.exit(1)

xls = pd.ExcelFile(path)
print("sheets", xls.sheet_names)
for s in xls.sheet_names:
    df = pd.read_excel(xls, sheet_name=s)
    print(f"\n--- {s} --- shape={df.shape}")
    print("columns", list(df.columns))
    print(df.head(5).to_string(index=False))
