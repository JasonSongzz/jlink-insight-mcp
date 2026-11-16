# CI/CD 集成

本文说明如何把「编译 → host 单元测试 →（可选）烧录 → 取证」接进流水线。

核心前提：**CI 里没有 LLM，也没有 MCP 客户端**。所以提供 `jlink-ci` 命令行入口，
用**退出码**表达结果，用 `--json` 输出结构化证据。

## 1. 退出码契约

| 退出码 | 含义 | 典型原因 |
|---|---|---|
| `0` | 成功 | 编译通过、测试全绿、烧录校验通过 |
| `1` | 执行失败 | 编译错误、host 测试失败、烧录/校验失败 |
| `2` | 用法或配置错误 | 缺 `--project`、`--flash` 未带 `--confirm`、`JLINK_SVD_DIR` 指向不存在的目录 |

CI 只需判断 `$?`；需要细节时用 `--json`。

## 2. 命令一览

```bash
jlink-ci status   --project .                       # 工具链 / ELF / host 测试就绪情况
jlink-ci build    --project . --preset Debug        # 仅编译
jlink-ci test     --project . --opt both            # 仅 host 单元测试（无硬件）
jlink-ci verify   --project .                       # build + test，CI 门禁（无硬件）
jlink-ci flash    --project . --chip STM32U575RG --confirm   # 真实写 Flash
jlink-ci pipeline --project . --flash --confirm --observe 30 # 全链路
jlink-ci debug    --project . --cmd bt --cmd "info registers"
```

通用参数：`--json`（纯 JSON 输出，可重定向成工件）、`-v/--verbose`（INFO 日志）、
`--timeout`（单步超时，秒）。

## 3. 无硬件 job（推荐所有提交都跑）

适用：编译 + 逻辑测试。需要 cmake / ninja / arm-none-eabi-gcc / host gcc，**不需要 J-Link**。

```yaml
jobs:
  firmware-verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          path: firmware
      - name: Install ARM toolchain + cmake/ninja + host gcc
        run: |
          sudo apt-get update
          sudo apt-get install -y cmake ninja-build gcc \
            gcc-arm-none-eabi libnewlib-arm-none-eabi
      - name: Install jlink-ci
        run: |
          git clone --depth 1 https://github.com/JasonSongzz/jlink-insight-mcp.git ci-tool
          python -m pip install --no-cache-dir ./ci-tool
      - name: Verify (build + host tests)
        working-directory: firmware
        env:
          JLINK_LOG_LEVEL: WARNING
        run: jlink-ci verify --project . --json > ../evidence.json
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: evidence
          path: evidence.json
```

要点：

- `--json > evidence.json`：把完整证据（工具链版本、ELF SHA256、每个用例结果）存成工件。
- 失败时 `failing_tests` 会出现在 JSON 里，同时日志尾部已打到 stderr，CI 控制台能直接看到断言。
- 不需要 `PATH` 里预置工具链路径：`jlink-ci` 会按 `manifest → EMBEDDED_* → PATH → 常见目录` 自动发现。

## 4. 有硬件 job（self-hosted runner）

适用：真实烧录 + 运行时取证。需要在 runner 上装 SEGGER J-Link Software 并插着探针。

```yaml
jobs:
  firmware-flash:
    needs: firmware-verify          # 逻辑没通过就不烧板
    runs-on: [self-hosted, jlink]   # 带 J-Link 的机器
    steps:
      - uses: actions/checkout@v4
        with:
          path: firmware
      - name: Flash + observe
        working-directory: firmware
        env:
          JLINK_LOG_LEVEL: WARNING
        run: |
          jlink-ci pipeline --project . \
            --flash --confirm \
            --chip STM32U575RG \
            --observe 30 \
            --json > flash-evidence.json
```

要点：

- **`--confirm` 是硬闸**：没有它直接退出 2，不会写 Flash。防止误把 pipeline 跑成烧录。
- `needs: firmware-verify` 让硬件 job 只在逻辑测试通过后才跑。
- 探针是独占资源：**不要并发跑两个硬件 job**。用 `concurrency` 组串行化：

```yaml
concurrency:
  group: jlink-probe
  cancel-in-progress: false
```

- 如果目标板是共享的，建议在 job 里先 `jlink-ci status` 确认 `debug_ready=true`。

## 5. 环境变量

| 变量 | 用途 |
|---|---|
| `JLINK_LOG_LEVEL` | `WARNING` 让 CI 日志干净；`DEBUG` 排查问题 |
| `EMBEDDED_CMAKE` / `EMBEDDED_NINJA` / `EMBEDDED_ARM_GCC` / `EMBEDDED_HOST_CC` | 非标准安装位置时显式指定 |
| `JLINK_SVD_DIR` | 需要寄存器按字段解析时指向 SVD 目录 |
| `EMBEDDED_JLINK_GDBSERVER` | GDB Server 不在 PATH 时指定 |

> 构建子进程会**自动剔除**名字含 `KEY`/`PASSWORD`/`SECRET`/`TOKEN` 的变量与 `DSH_*`，
> 避免 CI 密钥泄漏给 CMake 脚本或测试程序。确需传入时用 manifest `build.env_allow`。

## 6. manifest 里与 CI 相关的字段

```json
{
  "chip_name": "STM32U575RG",
  "firmware": "build/Debug/Application.elf",
  "build": {
    "preset": "Debug",
    "host_test_script": "Tests/run_host_tests.py",
    "host_cc": "",
    "env_allow": [],
    "env": {}
  }
}
```

`host_test_script` / `host_cc` 留空即自动探测。

## 7. 已知边界

- host 测试只覆盖硬件无关逻辑；外设时序、DMA 覆盖、Flash 延迟仍需目标硬件。
- 烧录需要独占探针，CI 上要保证同一时刻只有一个硬件 job。
- Windows runner 上 `taskkill` 用于终止超时进程树；Linux/macOS 用 `killpg`，两者都已处理。
- 没有 `--json` 时输出是给人看的摘要；解析请始终用 `--json`。
