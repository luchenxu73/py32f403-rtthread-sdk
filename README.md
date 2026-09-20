# PY32 RT-Thread SDK

本仓维护 PY32 的厂商 SDK 软件包、构建适配和本地验证工具。公开仓库地址为
[py32-rtthread-sdk](https://github.com/luchenxu73/py32-rtthread-sdk)，
维护邮箱为 `luchenxu73@gmail.com`。BSP 本体在 RT-Thread fork 的
`bsp/py32/py32f403_start_kit/`，本仓不保存 BSP 副本。

当前尚未向 packages 提交索引，也未完成板上验收。按当前开发安排暂不配置 CI。
临时产物、`AGENTS.md`、提示词及开发计划不进入提交；包导出采用文件白名单。

## 已确认的首板配置

| 项目 | 配置 |
| --- | --- |
| 板名 / MCU | `py32f403_start_kit` / PY32F403V1DT6（`PY32F403xD`） |
| Flash / SRAM | 384 KiB / 64 KiB |
| 时钟 | HSE 24 MHz 直驱 SYSCLK，PLL 关闭，AHB/APB1 24 MHz，APB2 12 MHz |
| 控制台 | uart2，PA2 TX / PA3 RX，AF2，115200 8N1 |
| LED | PA1，低有效，默认应用每 500 ms 切换亮灭 |
| 用户按钮 | PA0，按官方 Start Kit V2 定义；尚无 EXTI 驱动 |

启动阶段用 DWT 为 HAL 超时计时，调度启动后接入 RT-Thread tick（1000 Hz）。
外部晶振失败时明确停机供调试器检查，不静默回退至另一套时钟配置。

## 软件包与 BSP 的分工

- 本仓提供 `py32f403_sdk`：Device、system、启动文件和 HAL 合包，入口为根
  `SConscript`，显式调用 `sdk/py32f403/SConscript`。
- packages 仓只接收 `Kconfig`、`package.json` 及菜单接入；下载地址指向本仓
  固定提交或版本化 ZIP。索引模板在 `tools/package/`。
- RT-Thread 仓提交 BSP、共享驱动、链接脚本和分发适配，不提交厂商 SDK。
- CMSIS-Core 使用独立共享包；缺包时报错，不回退到厂商自带 Core。

`sdk/py32f403/RTThread/` 是新增的启动适配目录，保留官方启动原件用于比对。
GCC/Clang 的复位处理函数改为进入 RT-Thread `entry()`，由内核启动主线程后调用
应用 `main()`；MDK 继续使用原版启动文件及 `$Sub$$main` 接入。详细来源和差异见
[SDK 说明](sdk/py32f403/README.md)。

## 当前本地下载验证

需要 Python 3.11、SCons、kconfiglib 及 Env 的 Python 依赖。本机可使用
`../env-windows/.venv/Scripts/python.exe`；以下 `python` 均指该解释器。

在本仓执行，输出目录必须尚不存在：

```powershell
python tools/validate_download.py --env-root ../env-windows --bsp-source ../rt-thread/bsp/py32/py32f403_start_kit --output .cache/download-check
```

此命令导出候选 ZIP，建立隔离测试索引，在临时本机 HTTP 服务上提供 SDK，
然后由真实 Env 执行 `pkgs --update`。CMSIS-Core 从公开 Git 仓库拉取并固定 SHA。
验证包括下载摘要、逐文件清单、无软链接/junction、SDK 自动移入 `libraries/`
及 Env 自动生成的板卡包桥接脚本。不会替换已有 Env 索引或修改源 BSP。

成功后，日志、`download-validation.json` 和装好实体依赖的 `bsp/` 位于输出目录。
这覆盖实际包管理器的下载、解包和接入流程；公开 SDK 地址及上游索引的验收
仍需在相应版本发布后另做。脚本也接受 `--sdk-url <公开ZIP地址>` 与
`--sdk-sha256 <64位摘要>`，用于公开产物验收。

构建该干净副本（从 SDK 仓根目录执行）：

```powershell
$env:RTT_ROOT = (Resolve-Path ../rt-thread).Path
$env:ENV_ROOT = (Resolve-Path .cache/download-check/env).Path
$env:PKGS_DIR = (Resolve-Path .cache/download-check/index).Path
$env:RTT_CC = 'gcc'
$env:RTT_EXEC_PATH = 'D:/dev_tools/arm-gcc/bin'
Set-Location .cache/download-check/bsp
python -m SCons --pyconfig-silent
python -m SCons -j8
```

`rtconfig.h` 由 `.config` 生成，不手工修改。GCC 使用 Arm GNU Toolchain
15.2.Rel1。Clang 配置如下，MDK 则设置 `RTT_CC=keil` 并指向 Arm Compiler 6 的 bin：

```powershell
$env:RTT_CC = 'llvm-arm'
$env:RTT_EXEC_PATH = 'D:/dev_tools/ATfE-22.1.0-Windows-x86_64/bin'
$env:RTT_GNU_ROOT = 'D:/dev_tools/arm-gcc'
python -m SCons -j8
```

Clang 使用 LLD、ATfE compiler-rt 和 GNU newlib-nano 4.5.0。内部
`PLATFORM='gcc'` 用于选择 RT-Thread 现有 GNU ABI/newlib 适配，编译器仍为
Clang；不使用需要额外 TLS 初始化的默认 Picolibc。上游部分 GCC 专属警告选项会触发
Clang 警告，当前仅验收默认 C 工程，尚未验收 C++。

`tools/prepare.py local` 保留为可选的 SDK 联调映射工具；它不等价于下载验证。
当前主 BSP 的依赖已改为经过下载验证的实体副本，其来源记录在被忽略的
`packages/py32-download.json`。修改 SDK 后需重新导出、下载验证并更新依赖，
这些副本不会自动同步。此本地接入记录不声称主 BSP 原地运行过 `pkgs --update`。

## 发布与索引准备

```powershell
python tools/export_package.py
```

候选 ZIP、SHA-256 与逐文件清单输出到 `dist/`，记录源码提交及 dirty 状态。
ZIP 含唯一顶层目录，以符合 Env 解包约定；安装后包根仍有 `SConscript`。
输出裁剪厂商 Core、RTOS/RTOS2、Start Kit 示例和开发临时文件，仓内原件保留。
`--release` 要求干净且已提交的源码；候选包不代表已发布版本。

SDK 提交公开可获取后，使用其真实 40 位 SHA 生成测试索引：

```powershell
python tools/prepare.py index --env-root ../env-windows --sdk-revision <已发布SDK提交SHA>
```

默认 URL 为 `https://github.com/luchenxu73/py32-rtthread-sdk.git`，默认邮箱为
上述维护邮箱，均可通过参数覆盖。没有显式 SHA 时只准备配置菜单，不虚构可下载
版本。生成的独立索引位于 `.cache/package-index`，不会修改现有 Env 索引。
正式版本应补充 tag/版本菜单，再由维护者分别提交 packages 与 RT-Thread PR。

索引包含所需版本后，在板卡目录使用标准流程：

```text
pkgs --update
scons --pyconfig-silent
scons -j8
```

固定 Env 将 HAL SDK 安装到系列 `libraries/`，板卡 `packages/` 保存桥接入口，
CMSIS-Core 位于板卡 `packages/`。`scons --dist` 会将依赖实体化到分发产物。

## 验证范围

本地测试结果与日志保存在被忽略的 `.cache/`，不是 CI 或板上通过声明。
本轮 Start Kit 的三工具链及下载包分发构建结果见对应本地验证记录。

| 项目 | 范围 |
| --- | --- |
| GCC / Clang / MDK | 支持本地 SCons 编译、链接；尚未上板 |
| `--target=mdk5` | Arm Compiler 6 项目；IDE 构建待验收 |
| CMake / VS Code / cdb | 支持工程生成；CMake 使用 GCC |
| 启动 / GPIO / UART | GPIO 基础模式和读写，Serial V1 IRQ RX / 轮询 TX |
| 未实现驱动 | GPIO EXTI、UART DMA/TX IRQ/硬件流控及其他外设 |

硬件验收依次检查烧录复位、PA1 闪烁、UART2 banner/msh、`help`、`list_thread`
和 GPIO 读写。调试器与烧录方案尚待实物确定。

| 依赖 | 固定基准 |
| --- | --- |
| RT-Thread（BSP 改动之前） | `ed686ba95b5e8fb45910efc638544e43ffba89e2` |
| SDK 官方来源 | `PY32F403_Firmware_V1.4.5` |
| Env Windows | `fc857c5abb975af9a6c0df5d087a54e02adada3a` |
| Env scripts | `0fd51234e31a6bc8f901aadd7b7dbd633b774527` |
| packages 索引基线 | `16ed99eba0a7897311972254bafe0667afb145ee` |
| CMSIS-Core | `39d8e01f0be84b83a8f11d33756e82ce1ef07a84` |

BSP 与 SDK 新增实现当前仍为未提交工作区，上表基线不包含本次实现。
工具测试：`python -m unittest discover -s tools/tests -v`。

许可证：新增适配与工具使用 [Apache-2.0](LICENSE)；厂商源码保持各文件的
BSD-3-Clause/Apache-2.0 声明。
