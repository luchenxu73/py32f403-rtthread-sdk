# SPDX-License-Identifier: Apache-2.0

import os
from building import *

Import('rtconfig')

cwd = GetCurrentDir()
group = []

if GetDepend('PKG_USING_PY32F403_SDK'):
    # Relative source paths preserve SCons variant directories, so objects
    # stay in the BSP build tree even when this package is a directory link.
    template = os.path.join('Templates', 'PY32F403xx_Templates')
    hal = os.path.join('Drivers', 'PY32F403_HAL_Driver')
    device = os.path.join(cwd, 'Drivers', 'CMSIS', 'Device', 'PUYA',
                          'PY32F403', 'Include')

    src = [os.path.join(template, 'Src', 'system_py32f403.c')]
    if rtconfig.PLATFORM in ('gcc', 'llvm-arm'):
        src += [os.path.join('RTThread', 'startup_py32f403xx.S')]
    elif rtconfig.PLATFORM in ('armcc', 'armclang'):
        src += [os.path.join(template, 'MDK-ARM', 'startup_py32f403xx.s')]

    # Key on RT-Thread component symbols, never on BSP-private switches: the
    # package must behave the same for every board that consumes it.
    modules = ['hal', 'hal_cortex', 'hal_rcc', 'hal_rcc_ex', 'hal_flash']
    if GetDepend('RT_USING_PIN') or GetDepend('RT_USING_SERIAL'):
        modules += ['hal_gpio']
    if GetDepend('RT_USING_SERIAL'):
        # Vendor UART interrupt/error paths reference the DMA HAL even when
        # the board exposes only polling TX and interrupt-driven RX.
        modules += ['hal_uart', 'hal_dma']
    src += [os.path.join(hal, 'Src', 'py32f403_' + module + '.c')
            for module in modules]

    # CMSIS-Core is supplied by its RT-Thread package. Do not add the bundled
    # Drivers/CMSIS/Include path: mixing two Core versions hides incompatibility.
    group = DefineGroup('PY32F403_SDK', src,
                        depend=['PKG_USING_PY32F403_SDK'],
                        CPPPATH=[device, os.path.join(cwd, hal, 'Inc')],
                        CPPDEFINES=['USE_HAL_DRIVER'])

Return('group')
