# 可供 STM32 工程内 AI 助手参考的调试流程

以下内容可按需复制到 STM32 工程自己的 AGENTS.md / CLAUDE.md；不要覆盖已有内容。

- 用户请求烧录调试当前已编译工程时，先调用 JLink Insight MCP 的 inspect_project，再生成 plan_project_debug。
- 多个构建产物、多个 MCU 配置或多个探针时，明确选择，不猜测第一个。
- 调用 deploy_project 烧录并独立回读校验，通过后才运行。
- 通过 get_runtime_report 轮询后台监控。报告中的 success 是工具执行状态；系统是否有故障要看 cpu.assessment、scheduling.assessment、findings、quality 和会话 errors。
- 将报告分为已观测故障、待确认风险、数据不足；引用具体寄存器、事件、阈值或日志证据。
- 不将没有异常记录表述为没有任何错误；不从 RTT 文本或任务占比单独断言死锁。
- 需要断点、单步、SVD 或内存访问时先停止后台监控，再 halt_cpu，完成后 run_cpu。
- SystemView 需要固件实际集成及输出有效事件；缺少时退回可用的基础功能并说明缺失证据。
- 仅在用户请求编译监听时启用 start_firmware_watch；它会在构建产物变化后反复烧录目标。
