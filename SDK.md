# PY32F403 SDK

本目录以 Puya 官方 `PY32F403_Firmware_V1.4.5` 为来源，为 RT-Thread
提供 Device 与 HAL 合并软件包。固件发行版本 `V1.4.5` 与源码中的
HAL 版本宏 `V0.0.1`、Device 版本宏 `V1.0.0` 是不同的版本标识。

## 来源与许可证

- `Drivers/` 保留官方布局，共 148 个文件，包括 HAL/LL、Device 头文件、
  官方 Start Kit 驱动及随 SDK 携带的 CMSIS。
- `Templates/PY32F403xx_Templates/Src/system_py32f403.c`、
  `EIDE/startup_py32f403xx.s`、`MDK-ARM/startup_py32f403xx.s`
  从同版官方包按原相对路径补入，字节内容未修改。
- Puya/ST 源文件保留其 BSD-3-Clause 声明与版权；CMSIS 保留 Arm
  Apache-2.0 声明及 [原始许可证](Drivers/CMSIS/LICENSE.txt)。
  本包构建适配脚本使用 Apache-2.0，厂商文件继续遵循各自声明。

本机比对来源为 `D:/code/mcu/proj/py32/PY32F403_Firmware_V1.4.5`。
此路径只记录来源，不参与构建。补入文件的 SHA-256：

| 文件（相对于 `Templates/PY32F403xx_Templates/`） | SHA-256 |
| --- | --- |
| `Src/system_py32f403.c` | `14504777d19cf6ee08dee8abea1f026cc87a28981fea9266be415632e39d23f0` |
| `EIDE/startup_py32f403xx.s` | `dce54f57f960a44afdb08df915fcb3cabc346cbc0e6919d5e50f845120bbd12c` |
| `MDK-ARM/startup_py32f403xx.s` | `778356278c3cf1b695e6b904893e0c3c56214ffd9aac15009ca674e39df9e709` |

## 本地修改清单

在添加 RT-Thread 适配前，仓内 `Drivers/` 与官方包有两处文件差异，
本次原样保留，尚未验证其硬件行为：

1. `Drivers/PY32F403_HAL_Driver/Inc/py32f403_hal_rcc.h`
   增加 `RCC_LSE_DIS_TIMEOUT_VALUE`，值为 `2`。
2. `Drivers/PY32F403_HAL_Driver/Src/py32f403_hal_rcc.c`
   两处等待 LSE 关闭的逻辑使用上述 2 ms 阈值退出等待，替代原先
   使用 `RCC_LSE_TIMEOUT_VALUE` 并返回 `HAL_TIMEOUT` 的逻辑。

其余 146 个 `Drivers/` 文件与本机官方包逐文件 SHA-256 比对一致。

新增 RT-Thread 适配：

- 包根 `SConscript` 即 SDK 构建入口，与 at32、n32、wch 等 hal-sdk 包一致，
  两者之间不再有额外的 `sdk/` 层级。
- `SConscript` 选择 HAL 模块、唯一 system 文件及工具链启动文件，
  源文件使用相对路径以保持 SCons 构建目录隔离；模块选择只依据
  `RT_USING_PIN`、`RT_USING_SERIAL` 这类内核符号，不依赖任何 BSP 私有开关。
- `RTThread/startup_py32f403xx.S` 从原版 GCC 启动文件派生，保留厂商
  版权、向量表和 data/BSS 初始化，在 `SystemInit()` 后调用 RT-Thread
  `entry()`。原版的 `__libc_init_array()` 和直接调用 `main()` 不用于
  RT-Thread；启用 C++ 时由 RT-Thread 的 C++ 初始化组件执行构造函数。

`RTThread/` 是本仓新增的适配目录，不是 Puya 官方 SDK 的组成部分，也不是
另一份 RT-Thread 内核。单独存放这份启动文件是为了保留 `Templates/` 中的官方
原件，便于比较和升级；目录名及位置是本包的组织约定。仅把链接器入口改为
`entry` 不足以接入内核，因为 Cortex-M 复位仍执行向量表中的 `Reset_Handler`。
GCC/Clang 必须由该处理函数调用 `entry`，随后由 RT-Thread 主线程调用应用
`main`。MDK 原件通过 `__main` 和 RT-Thread 的 `$Sub$$main` 接入，无需同样修改。

## 软件包接口

| 项目 | 约定 |
| --- | --- |
| 包名 | `py32f403_sdk` |
| 配置项 | `PKG_USING_PY32F403_SDK` |
| 获取布局 | 整仓 checkout 或裁剪 ZIP；包根即 SDK 根，与其它 hal-sdk 包一致 |
| 内容边界 | Device 头文件、system、启动文件与 HAL 合包 |
| CMSIS-Core | 默认依赖 `PKG_USING_CMSIS_CORE` 共享软件包 |
| BSP 提供 | 芯片宏、`py32f403_hal_conf.h`、时钟与引脚配置、链接脚本 |

BSP 必须选择与芯片匹配的 `PY32F403xB`、`PY32F403xC` 或
`PY32F403xD` 宏。官方头文件虽然提及 x4/x6/x8，但本发行包没有这些
型号对应的 Device 头文件，当前接口不声明支持它们。

每次构建只添加共享 CMSIS-Core 的头文件路径。本目录脚本不加入
`Drivers/CMSIS/Include`，不在缺包时回退到 SDK 自带 Core。BSP 的
缺包检查还须验证共享包实际文件存在；仅启用配置项不代表包已下载。
仓内 `Drivers/CMSIS/RTOS`、`RTOS2` 保留原样但不参与构建，发布裁剪
由仓级工具处理。

默认编译 `hal`、`hal_cortex`、`hal_rcc`、`hal_rcc_ex`、`hal_flash`；
`BSP_USING_GPIO` 或 `BSP_USING_UART` 启用时增加 `hal_gpio`，
`BSP_USING_UART` 启用时增加 `hal_uart` 和其依赖的 `hal_dma`。
厂商 UART 头文件无条件使用 DMA 类型，中断错误路径也引用 DMA 函数；
编译这项依赖并不表示 BSP 已支持 UART DMA。其余 HAL/LL 源文件
不自动编译。BSP 的 HAL 配置头须与这些选择保持一致，并在 UART
头文件前包含 DMA 头文件。

## 启动与链接约定

| `rtconfig.PLATFORM` | 启动文件 |
| --- | --- |
| `gcc`、`llvm-arm` | `RTThread/startup_py32f403xx.S` |
| `armcc`、`armclang`（`RTT_CC=keil`） | 原版 `Templates/PY32F403xx_Templates/MDK-ARM/startup_py32f403xx.s` |

所有分支仅编译一份官方 `Templates/.../Src/system_py32f403.c`，其中
`SystemInit()` 保留芯片 trim 加载、FPU 与 VTOR 初始化。板级时钟配置
在 BSP 完成，SDK 的默认 HSI 为 8 MHz，默认 HSE 常量为 24 MHz，
此常量不代表实际板卡焊接了 24 MHz 晶振。

已确认的 `py32f403_start_kit` 使用 PY32F403V1DT6、384 KiB Flash、64 KiB
SRAM、24 MHz HSE、USART2 PA2/PA3 与 PA1 低有效 LED；这些选择由 BSP 固定。

GCC/LLVM 链接脚本必须提供 `_estack`、`_sidata`、`_sdata`、`_edata`、
`_sbss`、`_ebss`，保留 `.isr_vector`，启用 `RT_USING_USER_MAIN` 以
提供 `entry()`。MDK 使用厂商 `__main` 入口与 RT-Thread 的
`$Sub$$main` 接入机制；实际工程和命令行验收由 BSP 完成。

官方 Device 声明 Cortex-M4、单精度 FPU、3 位 NVIC 优先级。xB/xC/xD
分别为 128/256/384 KiB Flash，均为 64 KiB RAM；容量应按首板完整
芯片料号确认后由 BSP 链接脚本固定。官方 Start Kit 参考串口为
USART2（PA2 TX、PA3 RX、AF2、115200），LED 为 PA1，实际板卡配置
不由 SDK 强制。
