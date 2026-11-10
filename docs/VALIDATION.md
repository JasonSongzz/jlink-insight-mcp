# JLink Insight MCP 0.2.0 验证记录

## 已执行

- Linux / Python 3.12 环境，安装项目及开发依赖成功。
- `python -m pytest`：**176 passed**，4 项依赖/旧测试警告，无失败；总体语句覆盖率 **65.34%**，通过项目 60% 门槛。
- 新增工程、固件、访问互斥、监控、监听、协议分析及测试模块的 Ruff 检查通过；`git diff --check` 通过。
- 真实 stdio MCP 客户端完成 initialize、list_tools、inspect_project 和 get_runtime_report 调用，不连接硬件。
- 使用人工构造的真实 ELF32 ARM 文件格式验证 ELF 符号、PT_LOAD 地址及固件数据；不依赖编译器或假设文件名判断格式。
- 故障注入覆盖校验失败、不正确 MCU/通用核心连接、多探针歧义、USB 读取失败、RTT 短写、清理异常、损坏数据及丢包。
- 后台监控生命周期、设备独占、编译监听的稳定后部署/相同哈希不重复部署，以及原始轨迹导出均已测试。

## 尚未执行

- 没有连接真实 J-Link、STM32 开发板或用户固件；尚未验证各平台实际 J-Link DLL 行为、Flash 算法、时钟和 trace hooks。
- 没有验证 Windows / macOS 或所有 Python 版本。CI 配置提供 Python 3.10 / 3.12 测试矩阵，尚未在 GitHub 运行。
- 默认测试只覆盖 tests/unit 与 tests/mock；依赖专用硬件/厂商资源的集成及性能目录不计入本次通过数量。
- 不声称可证明不存在死锁、饥饿、实时截止期违约，亦不声称兼容所有 RTOS/SystemView 版本。

硬件验收流程见 HARDWARE_VALIDATION.md。测试通过表示软件流程和模拟故障处理可运行，不等于完成实体板卡验收。
