# 首次开发板验收

本交付版本通过无硬件测试；真实芯片、J-Link DLL 及固件 trace 适配需要在用户主机验证。建议先用可恢复的示例工程逐级验收。

| 阶段 | 操作 | 通过条件 |
|---|---|---|
| 本机环境 | 安装 Python、J-Link Software；连接带电开发板 | J-Link 官方工具能识别探针与确切芯片 |
| 工程识别 | inspect_project / plan_project_debug | MCU、固件路径及链接证据与工程一致 |
| 基础固件 | deploy_project(..., monitor=False) | 完整回读校验通过，程序正确运行 |
| 调试 | halt_cpu / read_registers / step_instruction / run_cpu | PC 等可读取、单步和恢复工作 |
| SVD | 配置实际型号 SVD，仅读取选定安全寄存器 | 字段解析与参考手册一致 |
| RTT 固件 | 启动 mode="rtt" | 日志与官方 RTT 客户端单独运行时一致 |
| SystemView 固件 | 启动 mode="systemview" | 有 sync、init、task_start_exec，时钟及任务名正确 |
| 调度场景 | 两个周期任务、已知运行比例、可控中断负载 | 统计趋势与 SEGGER GUI 独立采集结果吻合 |
| 故障场景 | 测试固件主动触发 fault 或输出 assert | 活动异常 / 状态位 / 日志线索正确区分 |
| 异常清理 | 停止会话、断开 USB、重复启动 | 错误明确、清理可见、可重新连接 |
| 编译监听 | 监听 ELF，重新编译两个不同版本 | 稳定后各烧录一次，相同哈希不反复烧录 |

SystemView GUI 和本服务应分别采集对照，避免同时消费同一 RTT 通道。对照时保持固件与时钟配置一致。首次实测若失败，保留 get_runtime_report、环境诊断、J-Link 软件版本、MCU、RTOS 版本及固件集成方式供定位。
