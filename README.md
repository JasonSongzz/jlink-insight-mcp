# JLink Insight MCP

让 Claude / Codex 根据 STM32 工程的实际能力，自动烧录已编译固件、启动运行，并通过 J-Link、RTT、SystemView 获取运行证据与诊断反馈。

当前版本 **0.2.0**，提供完整可安装源码与测试，尚未经真实开发板验证。

## 新机器 6 步检查清单

> 换一台电脑时按顺序走一遍。每步都有期望输出；不对就按「失败怎么办」排查。

| # | 做什么 | 验证命令 | 期望 | 失败怎么办 |
|---|---|---|---|---|
| 1 | 装 SEGGER J-Link Software | `where JLinkGDBServer.exe` | 打印一个路径 | 从 segger.com 下载安装；装完重开终端 |
| 2 | 装编译工具链 | `arm-none-eabi-gcc --version`、`cmake --version`、`ninja --version` | 各自打印版本 | 装 STM32CubeCLT 或 Arm GNU Toolchain + CMake；确保在 `PATH` |
| 3 | 克隆仓库 | `git clone https://github.com/JasonSongzz/jlink-insight-mcp.git` | 目录里有 `setup_mcp.py` | 检查网络 / 代理 |
| 4 | 一键装 Python 环境 | `python setup_mcp.py --mirror https://pypi.tuna.tsinghua.edu.cn/simple` | 约 66s，`build_ready = True`、`debug_ready = True` | 先 `--check` 只自检；卡住见下文「安装卡住怎么办」 |
| 5 | 配 MCP 客户端 | 粘贴脚本打印的 JSON/YAML，确认 `command` 是 venv 里 python 的**绝对路径**、`cwd` 是 clone 目录 | 重载后 `tools/list` 出现 **64** 个工具 | 路径必须绝对；Windows 用正斜杠或转义反斜杠 |
| 6 | 在固件工程放 manifest | 工程根目录建 `jlink-project.json`（参考 `examples/jlink-project.json`） | `build_status` 返回 firmware + SHA256 | 不建也能用，但每次要手传 `project_path` / `firmware_path` |

装完后的三条验证（在 Agent 里直接调工具）：

```text
toolchain_detect()                 → build_ready 与 debug_ready 都应为 true
build_status("D:/your/project")    → presets、工具链文件、ELF SHA256、Flash/RAM 占用
list_jlink_devices()               → 插着 J-Link 时至少返回一个探针
```

**缺哪个前置依赖，就少哪类能力**（服务永远能启动，缺的能力调用时报错）：

| 已安装 | 可用 | 不可用 |
|---|---|---|
| 仅 Python 环境 | 服务启动、64 个工具全部注册 | 全部硬件工具、全部编译工具 |
| ＋编译工具链 | `toolchain_detect` / `build_status` / `build_project` | 烧录、RTT、寄存器、`gdb_batch` |
| ＋J-Link | `deploy_project` / `rtt_read` / `runtime_snapshot` / `gdb_batch` | SVD 按字段解析（需 SVD 文件） |
| ＋SVD 目录（可选） | `read_register_with_fields` 等 | — |

## 运行流程

1. CubeMX / VS Code 工程编译完成，生成 ELF、HEX 或 BIN。
2. `inspect_project` 读取 `.ioc`、FreeRTOS 版本头文件、编译数据库及 ELF 符号。
3. `deploy_project` 选择明确的 MCU 与探针，烧录到 STM32 内部 Flash，逐字节独立回读校验。
4. 校验成功后复位、运行；在后台发现实际 RTT / SysView 通道，按能力选择监控模式。
5. 客户端通过 `get_runtime_report` 轮询任务运行占比、ISR 耗时、日志和故障证据，生成反馈。

| 工程 / 固件能力 | 可用功能 |
|---|---|
| 裸机，无 RTT | 烧录、运行、断点、单步、内存、CPU 寄存器、SVD；可选故障采样 |
| 已集成 RTT | 额外采集 RTT 日志，标记 assert / panic 等日志线索 |
| FreeRTOS，无 SystemView | 常规调试及已有 RTT；不会凭空生成任务调度历史 |
| FreeRTOS + SystemView + trace hooks | 额外采集任务 / ISR 事件、独占运行占比、Ready 到 Running 延迟及质量信息 |

文件存在、参与编译、最终链接、运行时输出是不同层次的证据。主机服务不自动修改用户固件，也不自动植入 RTOS 或 SystemView。

## 快速安装（一键，跨平台）

需要 Python 3.10+。解压/克隆后进入源码根目录，运行：

```bash
python setup_mcp.py
```

脚本会：创建 `.venv` → 以 editable 模式安装依赖 → 自检工具链 → 打印可直接粘贴的 MCP 客户端配置。
重复运行是安全的（复用已有 venv）；`--force` 重建，`--check` 只自检不安装，`--dev` 附带测试依赖。

**外部前置条件（脚本无法代装）：**

| 依赖 | 用途 | 缺失后果 |
|---|---|---|
| SEGGER J-Link Software | J-Link 驱动 / `JLinkARM.dll` / `JLinkGDBServer` | 所有硬件工具不可用 |
| `cmake` + `ninja` + `arm-none-eabi-gcc` | 编译（`build_*` / `workflow_pipeline`） | 仅编译不可用，烧录调试照常 |
| 芯片厂商 SVD 目录（可选） | 寄存器按字段解析 | 仅 SVD 工具不可用，可用 `JLINK_SVD_DIR` 指定 |

工具链路径**不需要手写**：服务按 `manifest → EMBEDDED_* 环境变量 → PATH → gcc 兄弟目录 → 常见安装目录` 自动发现。
换机器只需设 `EMBEDDED_CMAKE` / `EMBEDDED_ARM_GCC` 等环境变量，源码不动。

> 注意：环境变量名不要包含 `KEY` / `PASSWORD` / `SECRET` / `TOKEN`，也不要使用 `DSH_*` 前缀
> —— 部分 MCP 客户端（如 dsh-mcp-client）会清洗这些变量。

**安装卡住怎么办：**

- 脚本**不会**执行 `pip install --upgrade pip`。venv 自带 pip 已够用，而自升级在部分网络/代理环境
  会挂起数分钟（实测 >180s 无输出、CPU≈0）。这是首次安装最常见的卡点。
- 国内网络慢可指定镜像：`set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`（Windows）
  或 `export PIP_INDEX_URL=...`（Linux/macOS）。
- 需要代理时设 `HTTPS_PROXY` / `HTTP_PROXY` 后再运行脚本。
- 离线环境：先在联网机器 `pip download` 出依赖，再用 `--find-links` 安装。

## 手工安装（Windows / PowerShell）

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-cache-dir .
.\.venv\Scripts\python.exe -m jlink_mcp
```

Linux / macOS：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir .
.venv/bin/python -m jlink_mcp
```

最后一条命令启动 stdio MCP 服务，等待客户端请求，不能把它当交互式命令行输入 Python 函数。通常由客户端直接启动，无需手动运行。

安装后的新命令为 `jlink-insight-mcp`，兼容命令 `jlink-mcp` 也保留。Python 包路径仍为 `jlink_mcp`。

> 手工安装的两条纪律：**必须带 `--no-cache-dir`**（pip 缓存写入在部分环境下会挂起），
> **不要加 `-e`**（editable 元数据生成在部分 hatchling/pip 组合下会卡住）。
> 只有在需要「改源码即时生效」时才用 `python setup_mcp.py --editable`。

> ⚠️ 依赖里的 `mcp` 必须 `<2`：本包使用 `mcp.server.fastmcp`，`mcp` 2.x 已将其改名为 `MCPServer`，
> 装上 2.x 会在启动时抛 `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`。

## 客户端配置

把示例中的 Python 路径替换为本机解压目录下虚拟环境的绝对路径。

- Codex CLI / IDE：参考 [examples/codex-config.toml](examples/codex-config.toml)，合并到 `~/.codex/config.toml`。
- Claude Code / Claude Desktop：参考 [examples/claude-mcp.json](examples/claude-mcp.json)。
- VS Code 原生 MCP 客户端：参考 [examples/vscode-mcp.json](examples/vscode-mcp.json)，合并到工作区 `.vscode/mcp.json`。Claude / Codex 扩展使用各自的配置方式。

MCP 服务必须运行在能访问工程文件、J-Link USB 和 J-Link 软件的那台电脑上。云端执行环境不能自动访问本地开发板。SVD 文件从 `JLINK_SVD_DIR` 加载；未配置有效的型号对应文件时不会虚构寄存器定义。基本硬件工具不需要 OpenAI API Key；继承的可选语义检索功能除外。

Codex 配置字段依据 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

## 给 Claude / Codex 的使用指令

> 检查当前工程 C:/projects/demo，固件已经编译完成。识别 MCU、FreeRTOS 和 SystemView 能力，使用 JLink Insight MCP 烧录、校验并运行固件，监控 60 秒。定期查询报告，区分已确认故障、调度风险和数据不足，并说明观测证据。

有多个 ELF、多个 `.ioc` 或多个探针时，需要指定对应文件或探针序列号；服务不会直接选择第一个。

以下是 **MCP 工具调用参数示例**，不是终端命令：

```python
inspect_project(project_path="C:/projects/demo")
plan_project_debug(project_path="C:/projects/demo", firmware_path="build/Debug/demo.elf")
deploy_project(project_path="C:/projects/demo", firmware_path="build/Debug/demo.elf", duration_s=60)
get_runtime_report()
# 传入上次返回的 last_sequence，只获取新的消息。
get_runtime_report(after_sequence=1)
stop_runtime_monitor()
```

BIN 需要明确 `flash_address`，ELF / HEX 使用固件自带地址。推荐保留 ELF 符号供能力识别。可将 [examples/jlink-project.json](examples/jlink-project.json) 复制到 STM32 工程根目录，固定固件路径、MCU 和 BIN 地址。

## 编译完成后自动部署

显式启动编译产物监听：

```python
start_firmware_watch(
    project_path="C:/projects/demo",
    firmware_path="build/Debug/demo.elf",
    duration_s=3600,
    deploy_initial=False
)
get_firmware_watch_status()
stop_firmware_watch()
```

监听选定文件的 SHA256 变化，连续稳定一秒后部署。`deploy_initial=False` 会跳过启动时已有的文件，等待下次编译变化；设置 `True` 则部署当前稳定文件。每个哈希只尝试一次；失败后查看 `last_result`，修复问题并重新启动监听，或编译出新版本。不会执行构建命令。

监听器只自动停止自己上次部署产生的监控会话，遇到其他会话占用时报告错误。`stop_firmware_watch` 停止后续部署，当前固件与已启动监控仍继续；停止监控另调 `stop_runtime_monitor`。正在烧录时无法中断底层 DLL 调用，停止请求可能返回 `stop_pending=true`。

## 实时监控和诊断边界

- 后台监控与 MCP 会话同时运行，客户端需要轮询报告；本服务不承诺客户端会主动弹窗。
- 默认不暂停 CPU，避免干扰调度。此时通过 RTT / SystemView 观测运行行为；普通运行中的 CPU 故障寄存器检查标为 unknown。
- `sample_cpu=True` 会周期性短暂停机采样并恢复运行，影响时序。与 SystemView 同用时，报告会降低调度判断的可信度。
- 停止后台监控后，可用 `runtime_snapshot(halt_if_running=True)` 单次检查活动异常、CFSR / HFSR 等证据。M0 / M0+ 等不支持的扩展故障寄存器不会读取。
- 捕获到活动 fault 异常与故障状态位残留分别报告。日志模式匹配和阈值超限属于线索 / 风险，不能单独证明 HardFault、死锁或优先级反转。
- 没有异常记录只表示当前窗口未发现异常，不能证明系统永远正确。丢包、缺少时钟或 trace hooks 时明确提示数据不足。
- 监控窗口保留最近 20000 个解码事件、最近 100 行日志，原始轨迹最多 4 MiB；到达 4 MiB 或持续时间上限会停止并保留结果。单次监控 1–3600 秒，可显式重新启动。
- 当前面向单核 STM32 / Cortex-M / FreeRTOS。多核合并、RT-Thread 专项适配、完整 FreeRTOS API 锁关系分析以及 `.SVDat` 容器尚不支持。

监控期间其他硬件写入、断点、内存与 SVD 工具会报告设备占用。先停止监控，再调试：

```python
stop_runtime_monitor()
halt_cpu()
read_registers(register_names=["PC", "LR", "SP", "XPSR"])
# 用实际 SVD 名称、外设和寄存器替换参数。
read_register_with_fields(device_name="STM32F407", peripheral_name="GPIOA", register_name="MODER")
run_cpu()
```

SVD 外设读取可能有硬件副作用，应只请求需要的寄存器，不做自动全量扫描。CPU 是否运行、断点位置及程序错误之间也需要结合上下文解释。

## 新增 MCP 工具

| 工具 | 用途 |
|---|---|
| `inspect_project` | 工程、固件和集成能力识别 |
| `plan_project_debug` | 只读部署计划及 SVD 候选 |
| `deploy_project` | 自动连接、烧录、校验、运行、启动监控 |
| `start_runtime_monitor` | 已连接目标的后台监控 |
| `get_runtime_report` / `stop_runtime_monitor` | 查询反馈 / 停止监控 |
| `runtime_snapshot` | 可选暂停采样及 Cortex-M 故障检查 |
| `export_runtime_trace` | 原始 SystemView RTT 文件 + JSON 报告 |
| `start_firmware_watch` | 编译产物变化后自动部署 |
| `get_firmware_watch_status` / `stop_firmware_watch` | 查询 / 停止编译监听 |
| `systemview_capture` / `systemview_decode` | 限时前台采集 / 原始二进制离线解码 |
| `toolchain_detect` | 自动发现 cmake / ninja / arm-none-eabi-* / JLinkGDBServer 及版本 |
| `build_status` | 盘点 manifest、presets、工具链文件、compile_commands、ELF SHA256 与 Flash/RAM 占用 |
| `build_project` | configure + build，返回完整编译器输出、warning/error 计数、ELF hash |
| `host_test` | 用宿主编译器编译并运行工程自带单元测试（双档 -O0/-O3），失败时逐用例定位 |
| `workflow_pipeline` | 一次串起 build → **host 测试门禁** →（可选）flash/校验/运行 →（可选）短时取证 |
| `gdb_batch` | 拉起 JLinkGDBServer + `arm-none-eabi-gdb -batch` 做 `bt`/寄存器/内存/反汇编 |

基础连接、调试、RTT、SVD 等原工具继续保留；可用客户端工具列表查看完整接口。`reset_target` 支持 normal / halt；core-only reset 和范围擦除没有伪造的 pylink 调用。推荐使用 `deploy_project` 完成固件下载，避免手动整片擦除。

## 无 VSCode 的编译 / 烧录 / 调试工作流

新增的 5 个工具让这个服务可以独立承担「编译 → 烧录 → 取证 → 符号级调试」，
不依赖 VSCode / CubeIDE，可直接在任意支持 MCP 的 Agent 里驱动。

在工程根目录放 `jlink-project.json`（参考 [examples/jlink-project.json](examples/jlink-project.json)），
工具链路径留空即自动发现：

```json
{
  "ioc": "Application.ioc",
  "chip_name": "STM32U575RG",
  "firmware": "build/Debug/Application_FACTORY.elf",
  "flash_address": "0x08000000",
  "build": { "preset": "Debug", "firmware": "build/Debug/Application_FACTORY.elf" }
}
```

典型调用顺序：

```text
build_status(project_path)                        # 只读：工具链 / presets / ELF / 占用
build_project(project_path, preset="Debug")       # 编译，拿 ELF SHA256
host_test(project_path, opt="both")               # 宿主编译器跑单元测试，不碰硬件
deploy_project(firmware_path=…, chip_name=…)      # 烧录 + 逐字节校验 + 运行
get_runtime_report() / rtt_read()                 # 取证
stop_runtime_monitor()                            # 释放硬件
gdb_batch(project_path, commands=["bt"])          # 需要符号级回溯时
```

或者一条 `workflow_pipeline(project_path, flash=True, confirm=True, observe_s=30)` 走完中间三步。

### Host 单元测试门禁（烧录前拦截）

`workflow_pipeline` 在 **build 之后、flash 之前**插了一个 `host_test` 阶段：

```
build → host_test(-O0, -O3) → flash → observe
             │
             └─ 失败 → 直接返回 ok=false，message 写明 "firmware NOT flashed; failing: <用例名>"
```

- 需要工程里有 host 测试驱动（自动找 `Tests/run_host_tests.py`，可用 manifest `build.host_test_script` 指定）。
- 需要宿主编译器（`gcc` / `clang` / `cc`，可用 `build.host_cc` 或 `EMBEDDED_HOST_CC` 指定）。
- **两者缺任一 → 该阶段标记 skipped，不阻塞流水线**（不当成失败）。
- `host_test_opt="both"`（默认）跑 `-O0` 和 `-O3`，能抓出只在某个优化等级暴露的 UB / 缺 `volatile`。
- 失败时自动逐个 `--only <name>` 重跑，直接告诉你哪个用例挂了。

这套设计的意义：**逻辑回归永远到不了板子**。host 测试是秒级的，而烧录 + 上台架验证是分钟级且有硬件损耗。

## CI/CD：`jlink-ci` 命令行入口

CI 里没有 LLM 也没有 MCP 客户端，所以提供了确定性命令行入口，用**退出码**表达结果：

```bash
jlink-ci status   --project .                 # 工具链 / ELF / host 测试就绪情况
jlink-ci verify   --project .                 # build + host tests（无硬件，CI 门禁）
jlink-ci flash    --project . --chip STM32U575RG --confirm   # 真实写 Flash
jlink-ci pipeline --project . --flash --confirm --observe 30 # 全链路
jlink-ci debug    --project . --cmd bt        # 一次性符号级调试
```

| 退出码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 执行失败（编译 / 测试 / 烧录） |
| `2` | 用法或配置错误（缺参数、未带 `--confirm`） |

`--json` 输出结构化证据（含 `schema_version`、ELF SHA256、`failing_tests`），可直接存成 CI 工件。

无硬件 job 只需要 cmake/ninja/arm-none-eabi-gcc + 宿主编译器；烧录 job 放 self-hosted runner。
完整示例（GitHub Actions / self-hosted / 并发串行化）见 [docs/CI_INTEGRATION.md](docs/CI_INTEGRATION.md)。

**可移植性**：工具链按 `manifest → EMBEDDED_* 环境变量 → PATH → arm-none-eabi-gcc 兄弟目录 → 常见安装目录`
五级解析，代码中没有任何机器专属路径。换机器只设环境变量：

`EMBEDDED_CMAKE` · `EMBEDDED_NINJA` · `EMBEDDED_ARM_GCC` · `EMBEDDED_ARM_GDB` ·
`EMBEDDED_ARM_SIZE` · `EMBEDDED_ARM_OBJCOPY` · `EMBEDDED_JLINK_GDBSERVER`

**安全默认**：`workflow_pipeline` 的 `flash` 默认 `false`；打开时必须 `confirm=True`（真实写 Flash）。
全部子进程用 argv 列表 + `stdin=DEVNULL` + `capture_output`，不经过 shell，stdout 只留给 MCP 协议。

> 实现细节与已知坑（含 `stdin` 管道阻塞、`mcp<2` 约束）见 [docs/BUILD_WORKFLOW.md](docs/BUILD_WORKFLOW.md)。

## 测试与开发

```bash
python -m pip install --no-cache-dir ".[dev]"
python -m pytest
```

> 需要边改源码边跑测试时改用 editable：`python setup_mcp.py --dev --editable`
> （editable 元数据生成在部分环境会挂起，见上文说明）。

默认排除真实硬件集成及性能测试。显式运行全部单元与 Mock 测试（包括测试目录中的微基准）可用：

```bash
python -m pytest tests/unit tests/mock -o addopts="" -q
```

测试包括真实 ELF32 文件结构、镜像加载地址、回读失败中断、pylink 接口约定、SystemView 编码、嵌套 IRQ、丢包、资源互斥、后台监控、编译监听和真实 stdio MCP 客户端握手。硬件验收步骤见 [docs/HARDWARE_VALIDATION.md](docs/HARDWARE_VALIDATION.md)，架构见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

SystemView 固件接入详见 [docs/SYSTEMVIEW.md](docs/SYSTEMVIEW.md)。源码归属与许可见 [NOTICE.md](NOTICE.md) 和 [LICENSE](LICENSE)。
