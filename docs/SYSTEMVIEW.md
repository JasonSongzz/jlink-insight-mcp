# SystemView / FreeRTOS 运行状态分析

本扩展在原有 J-Link 连接上采集 SystemView **原始二进制 RTT 数据**，解码事件并生成可供 AI 分析的统计摘要。本文说明单核前台限时采集和固件接入；后台监控与编译监听见根目录 README。尚未进行开发板实测。

## 数据路径与接口

固件 FreeRTOS trace hooks → SEGGER SystemView 编码 → 名为 `SysView` 的 RTT Up 通道 → 主机采集 → 解码任务/中断事件 → 运行时间摘要。

| 工具 | 参数 | 作用 |
|---|---|---|
| `systemview_capture` | `duration_ms=1000`, `channel=None`, `block_address=None`, `max_bytes=65536`, `event_limit=100` | 请求 START，限时读取二进制数据，请求 STOP，返回原始 Base64 与统计 |
| `systemview_decode` | `data_base64`, `timestamp_hz=None`, `event_limit=100` | 离线解码原始 RTT 数据，不连接硬件 |

`channel` 是 Up 缓冲区索引；`block_address` 才是 `_SEGGER_RTT` 控制块地址。自动查找名为 `SysView` 的通道，不假定通道一定为 1；Up/Down 索引可以不同。当前要求唯一的 SysView Down 通道，多核录制尚未支持。

采集持续 1–10000 ms；开始前最多等待 2 秒进行 RTT 发现。默认数据上限 64 KiB，可设置为 1–256 KiB。达到时间或字节上限即结束。该工具是前台同步调用，在调用完成前服务器不会执行下一项 MCP 操作，需要后台采集时使用 `start_runtime_monitor`。

`raw_base64` 是无损原始 RTT 字节，不是 `.SVDat` 容器。当前不宣称可直接由 SystemView GUI 打开。离线输入上限 4 MiB、10 万解码事件；返回事件示例上限 1000，完整统计不受 `event_limit` 影响。

## FreeRTOS 固件接入

1. 确认 MCU、FreeRTOS 精确版本、编译器及现有 RTT 配置。
2. 从 [SEGGER 官方 SystemView 源码](https://github.com/SEGGERMicro/SystemView) 获取 SystemView、RTT 和匹配内核版本的 FreeRTOS 适配文件。工程已有 RTT 时使用同一套 RTT，避免重复定义 `_SEGGER_RTT`。
3. 按该适配包说明集成 trace 宏及必要的内核补丁。官方包含 `Sample/FreeRTOSV10.4` 等示例；不要直接套用于不同内核版本。必要时启用 `configUSE_TRACE_FACILITY`，并按适配器要求配置任务列表及 Idle 识别功能。
4. 配置 `SEGGER_SYSVIEW_Conf()`：时间戳来源、时间戳频率、CPU 频率和 RAM 基地址必须与工程一致。Cortex-M 的 DWT 不适用于所有型号，应根据内核能力配置。`timestamp_hz` 是时间戳时钟频率，并不总等于 CPU 频率。
5. 在开始调度和主机采集前调用 `SEGGER_SYSVIEW_Conf()`。主机发送 START，因此这一流程通常不需要固件预先调用 `SEGGER_SYSVIEW_Start()`。目标须持续产生事件或调用相应接口处理 Down 命令，否则 START/STOP 可能延迟生效。
6. 为 SystemView 保留足够的 Up/Down 缓冲区。固定通道时，RTT 最大缓冲区数量需大于该索引。高事件速率下增加 Up 缓冲区并缩短采集窗口。
7. 需要分析 ISR 时，确认 IRQ 入口、出口及切换到调度器路径均正确埋点，避免与 RTOS 适配重复记录。仅接入任务 trace hooks 不能自动获得所有外设中断事件。

这里不附带特定开发板固件，因为尚无 MCU 型号、FreeRTOS 版本及工程配置；已有固件若未集成 SystemView，主机端扩展不能单独还原调度历史。

## 使用

先在已安装 J-Link 软件的主机上安装修改版：

```bash
python -m pip install -e .
python -m jlink_mcp
```

通过 MCP 连接开发板，运行已集成 SystemView 的固件。以下为工具参数示例，工具由 MCP 客户端调用：

```python
connect_device(chip_name="STM32F407VG", interface="SWD")  # 替换为实际芯片
run_cpu()
systemview_capture(duration_ms=1000, max_bytes=65536, event_limit=50)
```

无需先调用普通 `rtt_start()`；若普通 RTT 已启动，先 `rtt_stop()`。停止其他 RTT 消费程序，包括 SystemView GUI/RTT Viewer，避免同一缓冲区被竞争读取。工具内部互斥只能保护本进程内 RTT 操作。

自动发现控制块失败时，可从 ELF/map 文件获取 `_SEGGER_RTT` 地址并传入 `block_address`。本扩展不会暂停或复位目标。烧录、断点暂停、复位和采集应分阶段进行，不要在录制期间从其他客户端操作目标。

返回值中：

- `data.analysis.tasks`：任务名称、优先级、wire ID、独占运行 ticks、微秒和窗口占比。
- `contexts`：任务、ISR、Idle 及 unknown 的独占耗时。嵌套 ISR 只向最内层归属耗时。
- `event_counts` / `events`：各类事件数量与有限数量的解码样本。
- `quality`：目标报告的丢事件数、丢弃字节、未完成尾包、解码错误和警告。
- `raw_base64`：留存后可用 `systemview_decode` 重算统计。
- `cleanup_warnings`：STOP 或 RTT 清理失败；应检查目标端录制状态。

`success=true` 表示主机采集流程完成，不代表目标已确认 START/STOP。通过 `trace_start`、`sync` 和有效事件确认已产生录制数据；空数据或缺少同步时，质量字段会提示。结束时不等待 STOP 事件，录制尾部可能是不完整包，原始数据保留供排查。

## 统计边界

- 统计窗口从同步后的事件到最后一个完整事件，不把主机等待时间视为目标执行时间，不外推最后一个事件之后的耗时。
- 占比表示**当前观测窗口的运行时间占比**。没有 Idle 识别或存在丢包时，不能据此报告精确的总 CPU 利用率。
- 发生 overflow 时，对丢失区间及后续尚未恢复的上下文记为 unknown，不继续归属于之前任务。
- task ID 保留线上压缩值，尚未恢复为 TCB 地址；删除后复用 ID 的任务不会分代区分，宜缩短窗口。
- 标准任务、IRQ、系统描述、时钟及栈信息事件可解码；栈信息是事件中上报的快照，不能推断整个窗口的最大栈深度。
- FreeRTOS API、自定义及扩展事件按长度保留 `payload_hex`，暂未加载 `.SVD` 事件描述文件（它与 CMSIS 外设 `.svd` 不同）。因此当前不自动诊断互斥锁死锁、优先级反转或完整的阻塞原因。
- 时间戳频率变化时禁用微秒换算。低位时间戳多次回绕造成的信息缺失无法由主机恢复。
- 当前不支持多核合并、GUI 时间轴导出或 `.SVDat` 容器导入导出。

## 后续扩展方向

建议顺序：持续流式落盘 → 真实硬件轨迹回归样本 → FreeRTOS 版本化 API 描述 → Ready 到 Running 延迟及阻塞原因 → Chrome Trace/Perfetto 时间轴 → 锁竞争等带证据的诊断规则。判断“死锁”“饥饿”等问题时须结合事件完整性、观测窗口和固件配置。

## 验证

```bash
python -m pytest tests/unit/test_systemview.py -o addopts='' -q
```

包含增量分包、官方 overflow 示例字节、变长事件、嵌套 IRQ、丢包、损坏数据同步恢复、时钟、浮点位模式、通道发现、异常清理和 MCP 注册测试。Mock 测试不替代真实 J-Link + 开发板验证。

实现参考 [SystemView 用户手册](https://doc.segger.com/UM08027_SystemView.html)，以及官方源码提交 `92ca7a810c5765ba64911919acd511c61b6b083f` 中的 `SYSVIEW/SEGGER_SYSVIEW.c`、`.h`、`SEGGER_SYSVIEW_Int.h`。本扩展未打包 SEGGER 源文件。
