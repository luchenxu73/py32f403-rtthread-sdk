# PY32F403 RT-Thread SDK

基于普冉官方 SDK，为 RT-Thread 提供 PY32F403 Device 与 HAL 软件包。

当前支持的开发板：
- py32f403_start_kit

## 快速开始

准备 RT-Thread Env 和对应编译器，按“编译构建”中的说明设置工具链。

在 Env 终端进入 RT-Thread 源码根目录，打开 BSP 配置菜单：

```shell
cd bsp/py32/py32f403_start_kit
scons --menuconfig
```

PY32F403 系列配置会自动选中 SDK 和 CMSIS-Core，SDK 版本可在以下菜单中查看或调整：

```text
RT-Thread online packages
    peripheral libraries and drivers
        HAL & SDK Drivers
            PUYA HAL & SDK Drivers
                -*- PY32F403 SDK (Device and HAL)
                    Version (latest)
```

保存配置后，下载依赖软件包：

```text
pkgs --update
```

SDK 安装到 `bsp/py32/libraries/py32f403_sdk-latest/`。执行以下命令构建固件：

```shell
scons -j8
```

## 编译构建

**GCC**：

```powershell
$env:RTT_CC = 'gcc'
$env:RTT_EXEC_PATH = 'C:/toolchains/arm-gnu/bin'
scons --pyconfig-silent
scons -j8
```

**Clang**：

```powershell
$env:RTT_CC = 'llvm-arm'
$env:RTT_EXEC_PATH = 'C:/toolchains/ATfE/bin'
$env:RTT_GNU_ROOT = 'C:/toolchains/arm-gnu'
scons --pyconfig-silent
scons -j8
```

Clang 使用 LLD、ATfE compiler-rt 和 GNU newlib-nano，因此仍需安装 Arm GNU Toolchain，
并将 `RTT_GNU_ROOT` 指向其根目录。

**MDK**（已验证 Arm Compiler 6.19）：

```powershell
$env:RTT_CC = 'keil'
$env:RTT_EXEC_PATH = 'C:/Keil_v5/ARM/ARMCLANG/bin'
scons --pyconfig-silent
scons -j8
```

构建产物为 `rtthread.bin`；GCC / Clang 同时生成 `rtthread.elf`，MDK 生成 `rtthread.axf`。
板卡 Flash 起始地址为 `0x08000000`，烧录后可通过 UART2 查看启动信息。

配套 BSP 还支持以下工程生成与分发命令，请先设置对应工具链：

| 命令                   | 用途                                          |
| ---------------------- | --------------------------------------------- |
| `scons --target=mdk5`  | 使用 MDK 工具链配置，生成 Arm Compiler 6 工程 |
| `scons --target=cmake` | 使用 GCC 配置，生成 CMake 工程                |
| `scons --target=vsc`   | 使用 GCC 配置，生成 VS Code 配置              |
| `scons --cdb`          | 生成编译数据库                                |
| `scons --dist`         | 生成包含 RT-Thread 和依赖软件包的 BSP 分发包  |

清理构建产物：

```shell
scons -c -n
scons -c
```

`-n` 预览清理列表。`scons -c` 清理当前构建图中的中间文件、
`rtthread.elf/axf/bin/map`、`build/compile_commands.json` 及上游登记的
`.sconsign.dblite` 等文件。执行前需配置工具链并更新软件包。

清理保留 `.config`、`rtconfig.h`、软件包、工程模板和导出的 CMake/IDE 工程；
不递归删除整个 `build/`，也不清理 `.config.old` 或 Python 缓存。
CMake 构建建议放在 `build/cmake/`，该目录不由 `scons -c` 清理。


## 支持范围

当前配套板卡为 `py32f403_start_kit`：

| 项目 | 配置 |
| --- | --- |
| MCU | PY32F403V1DT6，Cortex-M4F，`PY32F403xD` |
| Flash / SRAM | 384 KiB / 64 KiB |
| 时钟 | HSE 24 MHz，SYSCLK / AHB / APB1 24 MHz，APB2 12 MHz |
| 控制台 | UART2，PA2 TX / PA3 RX，115200 8N1 |
| 板载 LED | PA1，低电平点亮 |
| 用户按键 | PA0，GPIO 输入 |

SDK 保留厂商 HAL/LL 源码；配套 BSP 当前接入的功能如下：

| 功能 | 支持情况 |
| --- | --- |
| 启动与时钟 | RT-Thread 启动适配、HSE 时钟、SysTick |
| GPIO | 输入、输出、上下拉、开漏；尚未支持 EXTI |
| UART1 / UART2 | Serial V1，中断接收、轮询发送；尚未支持 DMA、发送中断和硬件流控 |
| 其他外设 | 尚未接入 BSP 驱动 |

| 工具链 | 命令行构建 | 硬件验证范围 |
| --- | --- | --- |
| GCC | 已验证 | DAPLink 烧录、Flash 回读、启动、UART2 控制台、SysTick、PA1 输出 |
| Clang | 已验证 | 未验证 |
| MDK Arm Compiler 6 | 已验证 | 未验证 |

上述范围适用于默认 C 工程。C++ 和 MDK IDE 内构建尚未验证。

SDK 来源、启动约定及厂商源码差异见 [SDK 说明](SDK.md)。新增适配与工具使用
[Apache-2.0](LICENSE)，厂商源码保留原有许可证声明。
