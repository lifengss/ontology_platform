# 本体平台 install-deps.ps1「捆绑 Python 3.10 安装失败」问题分析报告

| 项目 | 内容 |
|---|---|
| 报告日期 | 2026-10-30 |
| 涉及脚本 | `C:\Users\liwei\AppData\Local\Programs\OntologyPlatform\scripts\install-deps.ps1` |
| 失败环节 | Python 检测 → 静默安装捆绑 Python（`install-deps.ps1:299`） |
| 影响范围 | Python 运行时不可用 → venv、后端依赖、Postgres/Neo4j 后续阶段全部阻塞 |
| 当前状态 | ✅ 已修复，安装全流程跑通，`Status=OK` |
| 报告性质 | 已完成的修复归档 + 防复发加固说明 |

---

## 一、问题描述

### 1.1 用户侧现象

在 `02:31:26` 启动安装脚本后，界面停在下面这一步，超过 2 分钟没有任何输出：

```
[02:31:26] ===== 本体平台安装配置开始 InstallDir=C:\Users\liwei\AppData\Local\Programs\OntologyPlatform =====
[02:31:42] 端口 8123 空闲 → frontend = 8123
[02:31:45] 未检测到匹配 Python 3.10，静默安装捆绑 Python（约 1-2 分钟）...
```

随后抛出致命错误，定位到 `install-deps.ps1:299`：

```
python.exe : Python path configuration:
  PYTHONHOME = (not set)
  PYTHONPATH = (not set)
  program name = 'python'
  isolated = 0
  ...
ModuleNotFoundError: No module named 'encodings'
Fatal Python error: init_fs_encoding: failed to get the Python codec of the filesystem encoding
```

### 1.2 需要澄清的一点：进程没有「死掉」

用户第一反应是「是不是进程死掉了」。实测结论是**没有死**，而是进入了**不可收敛的等待/重试状态**：

| 观察项 | 实测结果 |
|---|---|
| 卡住那一刻 `python.exe` / `msiexec.exe` 是否存在 | 均不存在（安装器已退出，脚本在等待/兜底分支） |
| 当时唯一长时间停留的进程 | `CodeSetup-stable-4.10.4.2`（VS Code 自更新，PID 13704/17184，冻结约 20 分钟）——**与此问题无因果关系** |
| Python 安装器静默模式返回码 | `1603`（约 4 秒内返回），且**未生成 MSI 日志** |
| 脚本最终报错位置 | 超时分支（≈6 分钟）中的重试探活，第 299 行 |

错误处理流：安装器 4 秒返 1603 → 脚本认为「也许只是慢」继续等待 → 探活时执行 `python.exe -c ...` → 解释器缺 `Lib\` 直接崩溃 → 抛出 `No module named 'encodings'`。**所以这条报错是"结果"，不是"原因"。**

### 1.3 磁盘实际状态（故障现场）

目标目录 `...\OntologyPlatform\runtime\python\`（应含数百个文件与 `Lib\`、`DLLs\`、`<stdlib>` 等），当天实际只有 **6 个引导DLL/EXE**：

```
python.exe
pythonw.exe
python3.dll
python310.dll
vcruntime140.dll
vcruntime140_1.dll
```

没有 `Lib\`、没有 `DLLs\`、没有标准库 → 解释器连 `encodings` 都加载不了，必然 `init_fs_encoding` 致命错误。

---

## 二、根因分析

### 2.1 根因主线：一次「幽灵登记（Phantom Registration）」

系统注册表里，**Python 3.10.11 被登记为"已完整安装"**，而磁盘上只有 6 个引导文件。两者严重不一致：

| 注册表位置 | 故障时的值 | 说明 |
|---|---|---|
| `HKLM\SOFTWARE\Python\PythonCore\3.10\InstallPath` | `...\OntologyPlatform\runtime\python\` | 指向本安装目录 |
| `HKCU\SOFTWARE\Python\PythonCore\3.10` | 存在，且 8 个子组件全部登记 | Core / Standard Library / pip Bootstrap / Tcl-Tk / Dev Libraries / Utility Scripts / Documentation / Executables |
| `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*` | 4 条 `Python 3.10.11 ... (64-bit)` | Windows Installer 据此认为产品已到位 |

由此产生的**死循环**（这是整个故障的引擎）：

```
安装器启动 → 查到 3.10 已登记 → 进入"修复/维护"模式而不是全新安装
          → 发现磁盘文件缺失 → 不满足修复条件 → 4 秒返回 1603 且不留日志
          → 脚本重试 → 1603 → 重试 → ……永不到底
```

### 2.2 幽灵登记是怎么来的（诱因）

上一次安装被中断在最关键的组件上：

1. 安装进行到 **Standard Library（标准库）** 组件时被外力打断（该时段机器上有被中断的 VS Code 更新、以及先前那条**重启后待删除**登记——该文件 `clear-pending-delete.ps1` 已先行清理）；
2. Core Interpreter 已经写盘并登记成功，Standard Library 只铺了 6 个引导文件就中止；
3. 随后系统重启/重装，MSI 的回滚没能撤干净 → 留下「登记完备、磁盘残缺」的孤儿状态。

### 2.3 深层 MSI 故障：引擎自身在该组件上崩溃

直接对缓存 MSI 逐个安装取证，Standard Library 组件的 MSI 日志暴露了更底层的问题：

| 日志线索 | 含义 |
|---|---|
| `Internal Exception during install operation: 0xc00000fd` | 栈溢出（**MSI 引擎递归安装/长路径处理缺陷**），非磁盘、非权限问题 |
| `Note: 1324 2: .` | 目标路径解析异常（路径含尾随点，命中了该 assembly 的已知 MSI bug） |
| 重试后 `1729 Configuration failed` | 配置阶段再失败，彻底不可安装 |

结论：**这台机器上，走常规 `/quiet` 安装路径永远不可能收敛**。必须绕开安装引擎。

### 2.4 卸载也不通：子 MSI 的 LaunchCondition 形成依赖死锁

清理时发现，子组件 MSI 带启动条件：

> LaunchCondition: `No Python 3.10 installation was detected`

即**必须先把子组件（pip Bootstrap、Tcl/Tk、Standard Library 等）卸掉，最后才能卸 Core Interpreter**；一旦顺序反了（先删 Core），剩余子组件会因为「检测不到 Python 3.10」而拒绝卸载，全部返回 1603。

### 2.5 脚本自身缺陷（放大故障、并可能造成误伤）

`install-deps.ps1` 里有三处代码缺陷，把上面的环境问题放大成了"不可自愈"：

| # | 位置 | 缺陷 | 后果 |
|---|---|---|---|
| D1 | `Test-PythonRunnable`（原约 78 行） | 探活用了 `2>&1`。原生命令的 stderr 会被 PowerShell 包装成 `ErrorRecord` **混进成功输出流** | 一旦输出首行是 stderr 内容，`-match 'python'` 判定失败 → 把一个**其实完好的 Python 误判为不可用并删除** → 触发重装 → 1603 → 死循环 |
| D2 | 重装分支 | 重装前**没有**检测并清掉幽灵登记 | 带着"已安装"登记去重装，必然 1603 |
| D3 | 失败分支（约 316 行） | 未判断 `python.exe` 是否存在就直接调用 | 装不出来时抛出一句**看不懂的原生异常**，掩盖真实原因（1603） |

---

## 三、修复方案

### 3.1 已执行的修复动作（按时间顺序，全部落地）

| 步骤 | 动作 | 结果 |
|---|---|---|
| 1 | 清除 `HKCU\Python\PythonCore\3.10`、`HKLM\Python\PythonCore\3.10`（当时为幽灵值）、ARP 残留项、`C:\ProgramData\Package Cache` 下 14 项残留（先 DryRun 再执行） | 幽灵登记清除 |
| 2 | 在 `C:\Windows\Installer` 定位 Python 3.10.11 的 **8 个组件 MSI 载荷**（Core / Standard Library / Executables / pip Bootstrap / Tcl-Tk / Dev Libraries / Utility Scripts / Documentation） | 载荷齐全，可直接取用 |
| 3 | 逐个 `msiexec /i` 直装（带 `/L*V` 日志） | Core 等部分成功；**Standard Library 仍崩**（0xc00000fd / 1603） |
| 4 | **关键突破**：改用 `msiexec /a`（管理安装=纯文件解包，不写注册表、不跑 LaunchCondition、不跑 CustomAction） | 7 个组件成功解包（仅 pip Bootstrap 返 1603，无关紧要） |
| 5 | `robocopy /E` 将解包树部署到 `runtime\python` | 文件到位 |
| 6 | `python -m ensurepip --default-pip` 补齐 pip | `Successfully installed pip-23.0.1 setuptools-65.5.0` |
| 7 | 重跑 `install-deps.ps1` 全流程 | **一次通过，`Status=OK`** |
| 8 | 清理临时目录 `C:\p310x`、`C:\py310`、`~\Desktop\py310-layout` | 已确认全部删除 |

> 期间踩到的一个坑：`Copy-Item -LiteralPath (Join-Path $src '*')` 在 PowerShell 里**不做通配展开**，导致第一轮部署文件没拷过去（`TARGET_PYTHON_EXE=False`）。换成 `robocopy $src $target /E` 后解决。

### 3.2 脚本加固（已改入 install-deps.ps1，含备份）

原文件已备份：`C:\Users\liwei\Desktop\install-deps.ps1.bak`。改动如下：

| # | 修复 | 代码要点 |
|---|---|---|
| D1 | 探活不再污染 stdout | `( & $exe -c "..." 2>$null \| Out-String).Trim()`，退出码与输出分开判定 |
| D2 | 重装前先检测并卸载幽灵登记 | 检测 `HKCU/HKLM ...\PythonCore\3.$targetMinor`，命中则先 `"/uninstall","/quiet"`，等待 3 秒后再重装 |
| D3 | 失败提示可读 | 先判 `Test-Path $pyExeBundled`，缺失时输出 `(python.exe 未生成：<路径>)` |

校验结果：`BOM=True`、`SIZE=36772`、`SYNTAX_ERROR_COUNT=0`。

### 3.3 新增自愈脚本（兜底，防复发）

新增 `C:\Users\liwei\AppData\Local\Programs\OntologyPlatform\scripts\repair-python-bundled.ps1`。
当常规静默安装**无论如何修不好**时，完全绕开安装引擎：

1. 扫 `%windir%\Installer`，用 Windows Installer COM 读 MSI `Property.ProductName`，筛出 `Python 3.10.*` 的组件载荷；
2. 对每个载荷执行 `msiexec /a <msi> TARGETDIR=<staging> /qn` —— **纯解包**；
3. `robocopy /E` 部署到 `runtime\python`；
4. `ensurepip` 补 pip，最后打印 `REPAIR_OK`。

退出码约定：`0` 成功 / `3` 找不到载荷或 COM 不可用 / `4` 解包未产出 python.exe / `5` 部署后解释器仍不可运行。
该文件为**纯 ASCII**（避开 PowerShell 5.1 的 BOM/编码陷阱），语法校验 `SYNTAX_ERROR_COUNT=0`。

用法（管理员 PowerShell）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\liwei\AppData\Local\Programs\OntologyPlatform\scripts\repair-python-bundled.ps1"
```

---

## 四、修复后的验证结果

| 验证项 | 结果 |
|---|---|
| `logs\install-result.ini` | `Status=OK`；`frontend=http://localhost:8123 backend=http://localhost:8000` |
| Python 版本 | `3.10.11 (tags/v3.10.11:7d4cc5a, Apr 5 2023) [MSC v.1929 64 bit (AMD64)]` |
| pip | `pip 23.0.1` |
| `runtime\python\Lib` / `DLLs` | 存在 / 存在 |
| `runtime\venv\Scripts\python.exe` | 存在（venv 创建成功） |
| 后续阶段 | Postgres `initdb` 完成、Neo4j 解压并设密码、配置修补、快捷方式、目录授权全部完成 |
| 注册表一致性 | `HKLM\PythonCore\3.10\InstallPath` → `runtime\python\`，该路径下 `python.exe`、`Lib` **均存在** → **不再是幽灵登记** |
| 最终日志 | `===== 安装配置完成 =====`（09:09:38） |

---

## 五、残留项与后续建议

### 5.1 残留项（已核实为孤儿登记，暂不处理更安全）

| 项 | 状态 | 风险评估 |
|---|---|---|
| `HKLM\...\Uninstall` 下 4 条 `Python 3.10.11 * (64-bit)` | 孤儿 ARP 条目；其 ProductCode **未注册**到 `Installer\UserData\S-1-5-18\Products` | ⚪ 低。仅出现在"添加/删除程序"列表；点击卸载会报 1605。**不影响 msiexec 安装判定** |
| `HKCU\...\Uninstall\Python 3.10.11 (64-bit)` | 引导程序残留条目 | ⚪ 低，同上 |

**建议：保持不动。** 这些条目对应的 MSI 产品登记已不存在，手动强删不会带来实质收益，反而可能在误操作时触发 MSI 自修复、删掉我们用 `/a` 手工铺进去的文件。如确实需要清理列表观感，我可以做一次**只删这 5 个 ARP 键、绝不碰文件**的受控清理——**需要你先确认**。

### 5.2 防复发建议（按优先级）

| 优先级 | 建议 | 说明 |
|---|---|---|
| P0 | 安装过程避免并行其他安装器 | 本次诱因之一是机器上有被中断的 VS Code 更新；同一时刻只能有一个 MSI 事务在跑（MSI 本身有互斥锁，被外力打断就会留孤儿状态） |
| P0 | 把 `repair-python-bundled.ps1` 接进 `install-deps.ps1` 的 Python 阶段作为兜底 | 目前脚本只是"更健壮"，遇到同类崩溃仍会走到 6 分钟超时。建议改为：静默安装失败 → 自动调用 repair 脚本 → 再探活。**要不要接，等你一句话**（我不擅自改主流程） |
| P1 | 打包侧改为「绿色版 Python」而非 MSI 安装 | 用 embeddable package（`python-3.10.11-embed-amd64.zip`）解压 + `ensurepip`，从根本上消灭 MSI 依赖（没了 LaunchCondition、没了 1603、没了 Package Cache）。这是长期最优解 |
| P1 | `/quiet` 安装统一加 `/L*V` 日志到 `logs\` | 本次最大的诊断障碍是"1603 且不留日志"；有日志时能省下大量定位时间 |
| P2 | 安装前统一检查并引导清理「重启后删除」登记 | 与你先前那条 `clear-pending-delete.ps1` 的场景一致，建议内置到安装自检里 |

---

## 附录：关键命令速查

```powershell
# 1) 幽灵登记自检（有输出=需注意）
reg query "HKLM\SOFTWARE\Python\PythonCore\3.10" 2>nul
reg query "HKCU\SOFTWARE\Python\PythonCore\3.10" 2>nul

# 2) 解释器可用性探活（正常应输出 ...\runtime\python）
& "C:\Users\liwei\AppData\Local\Programs\OntologyPlatform\runtime\python\python.exe" -c "import sys,encodings,site;print(sys.prefix)"

# 3) 一键自愈
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\liwei\AppData\Local\Programs\OntologyPlatform\scripts\repair-python-bundled.ps1"

# 4) 兜底失败时带日志重装（用于取证）
msiexec /i <python-installer.exe> /quiet InstallAllUsers=0 TargetDir="..." /L*V "%TEMP%\py-inst.log"
```
