# 编译 / 调试工作流（headless）

本文说明新增的 5 个工具、可移植性设计、以及实现时踩到的坑。

## 1. 新增工具

| 工具 | 作用 | 碰硬件 | 关键参数 |
|---|---|---|---|
| `toolchain_detect` | 发现 cmake/ninja/arm-none-eabi-*/JLinkGDBServer，报版本与就绪标志 | ❌ | `project_path?` |
| `build_status` | 盘点 manifest、CMake presets、工具链文件、`compile_commands.json`、ELF SHA256、Flash/RAM 占用 | ❌ | `project_path`, `preset` |
| `build_project` | configure + build，返回完整编译器输出、warning/error 计数、ELF hash、size | ❌ | `project_path`, `preset`, `clean`, `target`, `timeout_s` |
| `host_test` | 用宿主编译器编译并运行工程自带单元测试，双档 -O0/-O3，失败逐用例定位 | ❌ | `project_path`, `script`, `cc`, `opt`, `only`, `timeout_s` |
| `workflow_pipeline` | build → **host 测试门禁** →（可选）flash/校验/运行 →（可选）短时取证 | ⚠️ 默认否 | `flash`, `confirm`, `observe_s`, `host_test_enabled`, `host_test_opt` |
| `gdb_batch` | 拉起 JLinkGDBServer + `arm-none-eabi-gdb -batch`，跑一次性命令 | ⚠️ 需独占 | `commands`, `elf_path`, `port` |

## 1.5 Host 单元测试门禁

### 为什么放在 build 与 flash 之间

```
build（ARM 交叉编译）
  ↓
host_test（宿主编译器，秒级，无硬件）
  ↓ 失败 → 立即返回 ok=false，message 含 "firmware NOT flashed; failing: <用例>"
flash（真实写 Flash，分钟级，有硬件损耗）
```

逻辑回归永远到不了板子。相比「烧进去跑一遍才发现」，省掉的是烧录时间、台架占用和误烧风险。

### 探测与调用

| 项 | 规则 |
|---|---|
| 驱动脚本 | 自动找 `Tests/run_host_tests.py`（也支持 `tests/`、`test/`、`scripts/`、根目录）；可用 manifest `build.host_test_script` 覆盖 |
| 宿主编译器 | `build.host_cc` → `EMBEDDED_HOST_CC` → PATH 上的 `gcc` / `clang` / `cc` |
| 调用协议 | `<python> <driver> --cc <cc> --opt <0\|3> --only <name>`（脚本不支持这些参数时无法集成） |
| 缺驱动或缺编译器 | 阶段标记 `skipped`，**不阻塞**流水线 |
| 显式配置了但找不到 | **报错**，不静默回退到 PATH 上另一个编译器 |

### 失败定位

整轮失败后，工具用 AST 读出驱动里 `TESTS = {...}` 的键，逐个 `--only <name>` 重跑，
返回 `failing_tests` 列表 —— 不用人工二分。

### 双档编译（-O0 / -O3）

同一份逻辑在两个优化等级下行为必须一致。常见能抓到的嵌入式 bug：

- 缺 `volatile`（-O0 里"碰巧能用"，-O3 被优化掉）
- 未初始化变量读到不同值
- 依赖求值顺序 / 有符号溢出等 UB
- 内联后暴露的别名问题

### 限制（写清楚，避免过度信任）

Host 测试只能覆盖硬件无关逻辑。以下仍需目标硬件：STM32 外设时序、DMA 半缓冲覆盖、
Flash 擦写延迟、NVIC 优先级、真实 ADC 噪声。测试输出里会带这条 note。


## 2. 可移植性：五级工具链解析

代码中没有任何机器专属路径。解析顺序：

```
1. jlink-project.json → build.toolchain.<key>    单工程覆盖
2. 环境变量 EMBEDDED_*                            换机器首选
3. PATH（shutil.which）
4. 已发现的 arm-none-eabi-gcc 的兄弟目录           arm_gdb / size / objcopy 自动推导
5. 常见安装目录 glob                               Windows / Linux / macOS
```

环境变量清单：

| 变量 | 对应工具 |
|---|---|
| `EMBEDDED_CMAKE` | cmake |
| `EMBEDDED_NINJA` | ninja |
| `EMBEDDED_ARM_GCC` | arm-none-eabi-gcc |
| `EMBEDDED_ARM_GDB` | arm-none-eabi-gdb |
| `EMBEDDED_ARM_SIZE` | arm-none-eabi-size |
| `EMBEDDED_ARM_OBJCOPY` | arm-none-eabi-objcopy |
| `EMBEDDED_JLINK_GDBSERVER` | JLinkGDBServer |

> 命名刻意避开 `DSH_*` 与 `KEY/PASSWORD/SECRET/TOKEN`：部分 MCP 客户端会清洗这些变量。

若只找到 part of toolchain，服务仍能启动：
`build_ready` 只要求 cmake + ninja + gcc；`debug_ready` 只要求 arm-gdb + JLinkGDBServer。

## 3. 工程 manifest

工程根目录 `jlink-project.json`：

```json
{
  "ioc": "Application.ioc",
  "chip_name": "STM32U575RG",
  "firmware": "build/Debug/Application_FACTORY.elf",
  "flash_address": "0x08000000",
  "build": {
    "preset": "Debug",
    "firmware": "build/Debug/Application_FACTORY.elf",
    "configure_args": [],
    "build_args": [],
    "env": {},
    "toolchain": {}
  }
}
```

`toolchain` 留空即自动发现。`configure_args` / `build_args` 追加到 cmake 命令尾部。

## 4. 构建逻辑

- 存在 `CMakePresets.json` 且指定 preset 在 `configurePresets` 中 →
  `cmake --preset <p>` + `cmake --build --preset <p>`（CubeMX 工程的默认形态）。
- 否则回退显式 configure：
  `cmake -S <root> -B build/<p> -G Ninja -DCMAKE_BUILD_TYPE=<p> -DCMAKE_TOOLCHAIN_FILE=<发现的工具链文件>`。
- 注入 `CMAKE_GENERATOR=Ninja` 与 `CMAKE_MAKE_PROGRAM=<发现的 ninja>`，避免 PATH 里多个 ninja 版本串台。
- 把 `arm-none-eabi-gcc` 所在目录前置进子进程 `PATH`，保证 CMake 能解析编译器。

## 5. 安全约束

| 约束 | 实现 |
|---|---|
| 默认不碰硬件 | `workflow_pipeline.flash` 默认 `False` |
| 写 Flash 必须显式确认 | `flash=True` 时要求 `confirm=True` |
| 不经过 shell | 全部 `subprocess.run([...], shell=False)` |
| 路径不出工程 | `_inside()` 校验相对/绝对路径都必须落在工程根内 |
| stdout 不污染协议 | 子进程一律 `capture_output`，stdout 只回传结果 |
| GDB 禁止拉起 shell | 过滤以 `shell ` 开头的命令 |
| 超时 | 每步独立超时，configure 最多 300s，build 默认 600s |

## 6. 已知坑（实现时真实踩过）

### 6.1 `mcp` 必须 `<2`

本包使用 `mcp.server.fastmcp`。`mcp` 2.x 将其改名为 `MCPServer`：

```
ModuleNotFoundError: No module named 'mcp.server.fastmcp'
```

`pyproject.toml` 已锁 `mcp>=1.26.0,<2`。

### 6.2 子进程必须 `stdin=DEVNULL`

MCP 服务的 stdin 是常开的 JSON-RPC 管道。`subprocess.run()` 默认让子进程继承 stdin，
cmake configure 里由 `execute_process` 派生的孙进程会继承该管道并**永久阻塞**：

```
现象：终端里直接 cmake 只要 0.5s；通过 MCP 调用时 configure 跑满 300s 超时
根因：stdio 传输下 stdin 是管道且常开
修复：所有子进程 stdin=subprocess.DEVNULL
```

这是本项目最隐蔽的一个坑，`_run()` 与 `_version()` 都已加。

### 6.3 wheel 不要重复 force-include resources

hatchling 的 `[tool.hatch.build.targets.wheel] packages = ["src/jlink_mcp"]`
已自动包含包目录下的非 `.py` 资源（SVD / JLink 补丁）。
再加 `force-include` 指向同一路径会构建失败：

```
ValueError: A second file is being added to the wheel archive at the same path
```

### 6.4 SVD 缓存目录可能不可写

系统级安装或只读镜像下，包内 `.svd_cache` 无法创建。`svd_manager` 现在按顺序尝试：

```
包内 .svd_cache → $JLINK_MCP_CACHE_DIR/svd_cache → ~/.cache/jlink_mcp/svd_cache
```

全部失败时禁用 pickle 缓存（仅报 warning，不阻止启动）。

### 6.5 SVD 资源不由本仓库分发

`resources/SVD_V1.5.6` 目录不在仓库中（体积与授权原因）。
寄存器解析需要设置 `JLINK_SVD_DIR` 指向芯片厂商 SVD 目录，例如 Keil DFP：

```
D:/Keil_v5/ARM/PACK/Keil/STM32U5xx_DFP/3.2.0/CMSIS/SVD
```

未配置时 `list_svd_devices` 返回空，其余工具不受影响。

### 6.6 改代码后需要重连 MCP

editable 安装让源码成为唯一真源，但**已运行的 MCP 进程内存里仍是旧代码**。
修改 `cordis.patch.yml` 中该条目的 `env`（任意值变化）可触发 dsh-mcp-client 原地重载。

## 7. 实测记录

| 项 | 结果 |
|---|---|
| 工具总数 | 64（原 59 + 新增 5） |
| 工具链发现 | 7/7，`build_ready=True`，`debug_ready=True` |
| 版本 | gcc 15.2.Rel1 / cmake 4.3.1 / ninja 1.13.2 / gdb 16.3.90 |
| configure | 0.52s |
| build（增量，含 MCUboot 签名校验） | 0.64 ~ 0.76s |
| 全新目录一键安装 | ✅ 通过（`setup_mcp.py`，从零建 venv） |
| 全新安装后真实编译 | ✅ 通过（64 工具、build ok、0 warning 0 error） |
