# JLink Insight MCP

让 Claude / Codex 根据 STM32 工程的实际能力，自动烧录已编译固件、启动运行，并通过 J-Link、RTT、SystemView 获取运行证据与诊断反馈。

基于 [cyj0920/jlink_mcp](https://github.com/cyj0920/jlink_mcp) 扩展，保留基础调试、SVD、GDB Server 和设备补丁功能。当前版本 **0.2.0**，提供完整可安装源码与测试，尚未经真实开发板验证。

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

## 安装（Windows / PowerShell）

需要 Python 3.10+、SEGGER J-Link Software，以及接在本机上的 J-Link 与已供电开发板。

解压后进入源码根目录：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m jlink_mcp
```

最后一条命令启动 stdio MCP 服务，等待客户端请求，不能把它当交互式命令行输入 Python 函数。通常由客户端直接启动，无需手动运行。

Linux / macOS：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m jlink_mcp
```

安装后的新命令为 `jlink-insight-mcp`，兼容命令 `jlink-mcp` 也保留。Python 包路径仍为 `jlink_mcp`。

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

基础连接、调试、RTT、SVD 等原工具继续保留；可用客户端工具列表查看完整接口。`reset_target` 支持 normal / halt；core-only reset 和范围擦除没有伪造的 pylink 调用。推荐使用 `deploy_project` 完成固件下载，避免手动整片擦除。

## 测试与开发

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

默认排除真实硬件集成及性能测试。显式运行全部单元与 Mock 测试（包括测试目录中的微基准）可用：

```bash
python -m pytest tests/unit tests/mock -o addopts="" -q
```

测试包括真实 ELF32 文件结构、镜像加载地址、回读失败中断、pylink 接口约定、SystemView 编码、嵌套 IRQ、丢包、资源互斥、后台监控、编译监听和真实 stdio MCP 客户端握手。硬件验收步骤见 [docs/HARDWARE_VALIDATION.md](docs/HARDWARE_VALIDATION.md)，架构见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

SystemView 固件接入详见 [docs/SYSTEMVIEW.md](docs/SYSTEMVIEW.md)。源码归属与许可见 [NOTICE.md](NOTICE.md) 和 [LICENSE](LICENSE)。
