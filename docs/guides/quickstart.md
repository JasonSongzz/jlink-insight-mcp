# JLink MCP 快速开始

## 安装方式

### 推荐：一键安装脚本

```bash
git clone https://github.com/JasonSongzz/jlink-insight-mcp.git
cd jlink-insight-mcp
python setup_mcp.py --mirror https://pypi.tuna.tsinghua.edu.cn/simple
```

脚本会创建 `.venv`、安装依赖、自检工具链，并打印可直接粘贴的 MCP 客户端配置。

外部前置依赖需自行安装（脚本无法代装）：

- **SEGGER J-Link Software**：提供 `JLinkARM.dll` / `JLinkGDBServer`
- **cmake + ninja + arm-none-eabi-gcc**：编译功能需要（装 STM32CubeCLT 也可）

完整检查步骤见 README 的「新机器 6 步检查清单」。

### 方式 2：手动安装

```bash
git clone https://github.com/JasonSongzz/jlink-insight-mcp.git
cd jlink-insight-mcp

# 创建 venv
python -m venv .venv

# 安装（Windows）
.venv\Scripts\python.exe -m pip install --no-cache-dir .

# 安装（Linux / macOS）
.venv/bin/python -m pip install --no-cache-dir .

# 启动 stdio MCP 服务（通常由客户端拉起，无需手动运行）
.venv\Scripts\python.exe -m jlink_mcp
```

三个注意点：

- **不要跑 `pip install --upgrade pip`**：venv 自带 pip 够用，自升级在部分网络/代理下会挂起数分钟。
- **不要用 editable 安装（`-e`）做日常使用**：部分 hatchling/pip 组合会卡在
  `prepare_metadata_for_build_editable`；只有需要改源码即时生效时才用 `--editable`。
- **包名是 `jlink-insight-mcp`，不是 `jlink-mcp`**；PyPI 上的 `jlink-mcp` 是上游旧包，功能与本文不符。

### 方式 3：使用 uv（可选）

```bash
git clone https://github.com/JasonSongzz/jlink-insight-mcp.git
cd jlink-insight-mcp
uv venv --python 3.12
uv pip install --no-cache-dir .
```

---

## iFlow CLI 配置

编辑 `.iflow/settings.json`:

```json
{
  "mcpServers": {
    "jlink-mcp": {
      "command": ".venv\\Scripts\\python.exe",
      "args": ["-m", "jlink_mcp"],
      "timeout": 60000
    }
  }
}
```

启动 iFlow:
```bash
iflow
```

---

## 工具概览 (46个)

### 连接管理 (5个) - 设备发现与连接

| 工具 | 功能 |
|------|------|
| `list_jlink_devices` | 枚举所有已连接的 JLink 设备 |
| `connect_device` | 连接到指定设备（支持芯片名称智能匹配） |
| `disconnect_device` | 断开当前设备连接 |
| `get_connection_status` | 获取当前连接状态和电压信息 |
| `match_chip_name` | 智能匹配芯片完整名称 |

### 设备信息 (4个) - 芯片信息查询

| 工具 | 功能 |
|------|------|
| `get_target_info` | 获取芯片型号、内核、Flash/RAM 大小 |
| `get_target_voltage` | 获取目标芯片供电电压 |
| `scan_target_devices` | 扫描 JTAG/SWD 总线上的设备 |
| `list_device_patches` | 列出已加载的设备补丁 |

### 内存操作 (4个) - 内存和寄存器访问

| 工具 | 功能 |
|------|------|
| `read_memory` | 读取指定地址内存（最大 64KB） |
| `write_memory` | 写入数据到指定地址 |
| `read_registers` | 读取 CPU 寄存器 |
| `write_register` | 写入单个寄存器 |

### Flash 操作 (3个) - 固件烧录

| 工具 | 功能 |
|------|------|
| `erase_flash` | 擦除 Flash（支持整片或指定范围） |
| `program_flash` | 烧录固件到 Flash |
| `verify_flash` | 校验 Flash 内容 |

### 调试控制 (7个) - CPU 控制

| 工具 | 功能 |
|------|------|
| `reset_target` | 复位目标芯片 |
| `halt_cpu` | 暂停 CPU |
| `run_cpu` | 运行 CPU |
| `step_instruction` | 单步执行 |
| `get_cpu_state` | 获取 CPU 状态 |
| `set_breakpoint` | 设置断点 |
| `clear_breakpoint` | 清除断点 |

### RTT (5个) - 实时日志

| 工具 | 功能 |
|------|------|
| `rtt_start` | 启动 RTT |
| `rtt_stop` | 停止 RTT |
| `rtt_read` | 读取 RTT 日志 |
| `rtt_write` | 写入数据到 RTT |
| `rtt_get_status` | 获取 RTT 状态 |

### GDB Server (3个) - GDB 调试

| 工具 | 功能 |
|------|------|
| `start_gdb_server` | 启动 GDB Server |
| `stop_gdb_server` | 停止 GDB Server |
| `get_gdb_server_status` | 获取 GDB Server 状态 |

### SVD (5个) - 寄存器解析

| 工具 | 功能 |
|------|------|
| `list_svd_devices` | 列出支持 SVD 的设备 |
| `get_svd_peripherals` | 获取设备外设列表 |
| `get_svd_registers` | 获取外设寄存器列表 |
| `read_register_with_fields` | 读取并解析寄存器字段 |
| `parse_register_value` | 仅解析寄存器值 |

### 使用指南 (5个) - 内置帮助

| 工具 | 功能 |
|------|------|
| `get_usage_guidance` | 获取工具使用指南 |
| `get_best_practices` | 获取最佳实践 |
| `list_scenarios` | 列出可用场景 |
| `get_forbidden_operations` | 获取禁止操作列表 |
| `get_system_prompt` | 获取系统提示词 |

### 配置诊断 (3个) - 服务状态自检

| 工具 | 功能 |
|------|------|
| `get_server_config` | 获取当前服务器运行时配置 |
| `get_server_capabilities` | 获取补丁、SVD、GDB Server 等能力状态 |
| `diagnose_environment` | 诊断资源路径、缺失项和修复建议 |

---

## 快速示例

### 连接设备

```python
# 列出设备
list_jlink_devices()

# 连接 (支持芯片名称简化)
connect_device(chip_name="FC7300F4MDD")  # 自动匹配完整名称
```

### 读取内存

```python
# 读取内存
read_memory(address=0x20000000, size=16)

# 读取寄存器
read_registers()
```

### Flash 操作

```python
# 擦除
erase_flash(chip_erase=True)

# 烧录
program_flash(address=0x08000000, data=firmware_data)
```

---

## 项目结构

```
jlink_mcp/
├── src/jlink_mcp/          # 源代码
│   ├── tools/              # 工具函数 (46个)
│   ├── models/             # 数据模型
│   ├── plugins/            # 插件 (Flagchip)
│   └── resources/          # 静态资源
├── docs/                   # 文档
│   ├── guides/             # 使用指南
│   └── reference/          # API 参考
├── tests/                  # 测试
│   ├── unit/               # 单元测试
│   ├── integration/        # 集成测试
│   └── mock/               # Mock 测试
└── pyproject.toml          # 项目配置
```

---

## 相关文档

- [API 文档](../reference/api.md)
- [使用示例](../reference/examples.md)
- [常见问题](../reference/faq.md)
- [UV 使用指南](./uv-guide.md)
- [发布指南](./release.md)

---

## 环境变量配置

可选的环境变量：

| 变量 | 说明 | 示例 |
|------|------|------|
| `JLINK_SVD_DIR` | 外部 SVD 文件目录 | `C:\svd_files` |
| `JLINK_PATCH_DIR` | 外部设备补丁目录 | `C:\patches` |
| `JLINK_DEFAULT_INTERFACE` | 默认接口类型 | `SWD` |
| `JLINK_GENERIC_CORE_FALLBACK` | 是否允许回退到通用 Cortex 核心 | `true` |
| `JLINK_DEFAULT_CORE` | 默认通用核心名称 | `Cortex-M4` |
| `JLINK_RESOURCE_MODE` | 资源模式配置 | `mixed` |

**设置方式 (Windows)**：
```powershell
$env:JLINK_SVD_DIR = "C:\svd_files"
```

**设置方式 (Linux/Mac)**：
```bash
export JLINK_SVD_DIR="/path/to/svd"
```

---

## 硬件验证状态

**FC7300F4MDSXXXXXT1C** (Flagchip)
- 连接: J-Link V11 via JTAG
- 通过: 连接、复位、寄存器读取

**当前版本**: v0.1.1

---

## 快速验证

### 验证安装

```bash
# 测试导入
python -c "import jlink_mcp; print('OK')"

# 启动服务器
jlink-mcp
```

### 验证连接

```python
# 列出设备
list_jlink_devices()

# 连接测试
connect_device(chip_name="FC7300F4MDD")

# 获取信息
get_target_info()

# 断开
disconnect_device()
```

### 验证功能

```python
# 连接
connect_device(chip_name="FC7300F4MDD")

# 复位
reset_target()

# 读取寄存器
read_registers(register_names=["R0", "PC"])

# 断开
disconnect_device()
```


