# -*- coding: utf-8 -*-
"""Автоопределение загрузчика по выводу UART для pytty-ai.

Единая точка входа — feed(). Вызывается из Terminal.insert_remote().
Данные профилей вшиты в код (чтобы работать в exe-сборке PyInstaller).

Использование:
    import boot_profiles as bp
    bp.feed(text)                    # каждый кусок вывода терминала
    bp.current()                     -> dict | None   (активный профиль)
    bp.prompt_hint()                 -> str           (блок в system prompt)
    bp.intercept_hint()              -> str           (как войти в консоль)
    bp.reset()                                       (при переподключении)
"""

WINDOW_SIZE = 8192
MIN_SCORE = 8

PROFILES = [
    {
        "id": "realtek_proprietary",
        "name": "Realtek proprietary bootloader (RTD129x/139x/161x, ТВ-боксы/ТВ)",
        "prompt_signature": "Realtek>",
        "markers": ["Realtek>", "Enter console mode, disable watchdog",
                    "Enter standby, skip this entry", "Audio FW", "Video FW",
                    "Android Verified Boot 2.0", "do_avb_init",
                    "boot manual mode", "RTD129", "RTD139", "RTD161",
                    "RTD131", "RTD2851", "RTD2995"],
        "weight": 10,
        "intercept_key": "пробел в первые секунды после включения (окно после 'Enter console mode, disable watchdog')",
        "commands": ("help, printenv, setenv, saveenv, rtkmmc, reset, "
                     "wdt [on|off], tftp, tftpput, swu/swuu (обновление FW), "
                     "usb, sleep, showvdq, uart2rbus, timelog, write, "
                     "spanel, tuner, vsyncapad. bootargs = командная строка "
                     "ядра (printenv)."),
        "forbidden": ("НЕ генерировать Linux-команды (cat, ls, fw_printenv, dd, "
                      ">, ||, ;). mmc -> rtkmmc. resetr -> reset. mtest отсутствует."),
        "ai_hint": ("Это НЕ Linux shell, это проприетарный загрузчик Realtek. "
                    "Только команды из списка. Ядро ещё не загружено, /proc нет."),
    },
    {
        "id": "realtek_uboot",
        "name": "Realtek U-Boot 2012.07 (RTD1295 и аналоги)",
        "prompt_signature": "Realtek>",
        "markers": ["U-Boot 2012", "Board: Realtek", "rtk_emmc",
                    "RTD1295 eMMC", "Kylin", "Cortex-A53"],
        "weight": 12,
        "intercept_key": "Esc при 'Hit any key to stop autoboot'",
        "commands": ("help, printenv/setenv/saveenv, bdinfo, chip, "
                     "rtkemmc/rtknand/rtkspi, boot_part, fastboot, factory, "
                     "keyset, uart_write, eeprom, loadb/loady, tftpboot, "
                     "usb start + fatload, md/mm/mw/cmp/cp/crc32, go, "
                     "boot/bootm/bootr, reset, pwm, rpmb, pmic."),
        "forbidden": ("Адреса загрузки брать из printenv (kernel_loadaddr, "
                      "fdt_loadaddr и т.д.). Перед fatload обязателен 'usb start'."),
        "ai_hint": "Realtek-вариант U-Boot 2012.07. Адреса — из переменных окружения.",
    },
    {
        "id": "mtk_uboot_router",
        "name": "MediaTek U-Boot routers MIPS (MT7620/7621/7628)",
        "prompt_signature": None,
        "markers": ["Ralink/MediaTek U-Boot for MIPS SoC", "MT7621", "MT7620",
                    "MT7628", "RT5350", "ASUS MT7621", "Xiaomi MT7621",
                    "Please choose the operation"],
        "weight": 10,
        "intercept_key": ("клавиша 4 в меню 'Please choose the operation' "
                          "(3 = U-Boot command line) или любая клавиша при autoboot"),
        "commands": ("help/?, printenv/setenv/saveenv, tftpboot, erase + cp.b, "
                     "bootm, go, md/mm/mw/cmp, loadb/loady, httpd "
                     "(веб-восстановление 192.168.1.1), mtkrecovery, reset, version."),
        "forbidden": ("U-Boot 1.1.4 синтаксис. Прошивка: tftpboot 0x80060000 fw.bin -> "
                      "erase -> cp.b -> bootm. Сначала проверка через bootm/go из RAM "
                      "без записи. mtest/gpt/mmc отсутствуют (SPI/NOR/NAND)."),
        "ai_hint": "Роутерный MediaTek U-Boot старого стиля. Флеш NOR/NAND, не eMMC.",
    },
    {
        "id": "mtk_genio_arm",
        "name": "MediaTek Genio/Filogic ARM (MT7981/7986/7988, MT7622)",
        "prompt_signature": None,
        "markers": ["F0: 102B 0000", "FA: 1040 0000", "F9: 103F 0000",
                    "MT7981", "MT7986", "MT7988", "MT7622",
                    "ARM Trusted Firmware", "BL2", "BL31", "FIP"],
        "weight": 9,
        "intercept_key": "любая клавиша при 'Hit any key to stop autoboot'",
        "commands": ("help, env print/set/save, mmc info/list/read/write, "
                     "gpt read mmc 0, ubi info/part, booti, bootm/boot, "
                     "tftpboot, mtd read/write, mtkboardboot, gpio/fdt/coninfo, "
                     "cp/cmp/md/mw/mm/crc32, reset."),
        "forbidden": ("Строки 'F0: 102B 0000' — bootrom ATF, норма, не ошибка. "
                      "printenv может отсутствовать — использовать 'env'."),
        "ai_hint": "Современный U-Boot с ATF (MTK FIP).",
    },
    {
        "id": "mtk_phone_preloader",
        "name": "MediaTek phone preloader/LK (MT67xx/68xx)",
        "prompt_signature": None,
        "markers": ["MTK PreLoader", "BROM", "Download Agent",
                    "Preloader USB VCOM", "MTK USB Port", "FASTBOOT",
                    "fastboot mode", "META mode", "Factory mode",
                    "little kernel", "LK"],
        "weight": 8,
        "intercept_key": "Volume Up+Power (fastboot); UART-консоли обычно нет",
        "commands": "fastboot devices/flash/boot/reboot (по USB, не UART).",
        "forbidden": ("В preloader нет интерактивной консоли — только SP Flash Tool "
                      "/ mtkclient / fastboot по USB."),
        "ai_hint": "Телефонный чип MediaTek. НЕ предлагать UART-команды, UART молчит.",
    },
    {
        "id": "mstar_tv",
        "name": "MStar TV (MSD6A338/628/638/828/938 — BBK, Ergo, Digma, UMC/Sharp)",
        "prompt_signature": "Mstar#",
        "markers": ["Mstar#", "Mstar>", "MBOOT", "MBoot", "mboot", "MSD6A",
                    "UART BUS OFF", "MStar Manhattan",
                    "recovery_wipe_partition"],
        "weight": 10,
        "intercept_key": ("ЗАЖАТЬ Enter ДО подачи питания и держать до Mstar#. "
                          "'UART BUS OFF' = UART выключен в Android: Menu 1147 -> "
                          "DEBUG -> MSTAR FAC MENU -> WDT=Off -> Other -> UART BUS=On"),
        "commands": ("help, printenv/setenv/saveenv, recovery_wipe_partition cache "
                     "(лечение зависания на заставке), recovery_wipe_partition data "
                     "(полный сброс ТВ), boot/bootm, mmcsd/mmc, update, reset."),
        "forbidden": ("recovery_wipe_partition data стирает пользовательские данные — "
                      "предупреждать. Linux-команд нет."),
        "ai_hint": ("MStar mboot (Mstar#). Bootloop: сначала wipe cache -> reset, "
                    "затем data. 'UART BUS OFF' — не сбой связи, а заблокированный "
                    "UART, включается из сервисного меню."),
    },
    {
        "id": "amlogic_tv",
        "name": "Amlogic TV/Box (T950/T962/T962X/T966/T968, S905*/S928X)",
        "prompt_signature": None,
        "markers": ["GXBB:BL1", "GXL:BL1", "GXM:BL1", "G12A:", "G12B:",
                    "T962", "T966", "T968", "Amlogic", "aml_autoscript",
                    "BL2:", "amlmmc"],
        "weight": 9,
        "intercept_key": ("любая клавиша при 'Hit Enter key to stop autoboot'; "
                          "прошивка с SD: aml_autoscript в корень + reset/toothpick"),
        "commands": ("help, printenv/setenv/saveenv, amlmmc read|write, "
                     "store read|write, fatls/fatload usb 0:1 (перед этим usb start), "
                     "booti (ядра 5.10+), bootm (uImage), run update, "
                     "md/mm/mw/cp/cmp/crc32, reset."),
        "forbidden": ("'GXBB:BL1/GXL:BL1' — bootrom, норма. bootm только для старых "
                      "uImage, новые ядра — booti."),
        "ai_hint": "Amlogic U-Boot. Прошивка с SD через aml_autoscript.",
    },
    {
        "id": "mediatek_tv",
        "name": "MediaTek TV (MT5596/97/98, MT9612/13, MT9652/53, MT9970, MT5893, MT9632)",
        "prompt_signature": None,
        "markers": ["MT5596", "MT5597", "MT5598", "MT9612", "MT9613",
                    "MT9652", "MT9653", "MT9970", "MT5893", "MT9632",
                    "Mediatek", "MEDIATEK", "MTK_TV"],
        "weight": 9,
        "intercept_key": "Enter при включении (если не заблокировано); часто консоль только на чтение лога",
        "commands": ("help, printenv/setenv/saveenv, mmc, bootm/booti, reset — "
                     "набор зависит от вендора ТВ, начинать с help."),
        "forbidden": ("Консоль плохо документирована, часто закрыта заводским "
                      "бинарником. Не предполагать Linux."),
        "ai_hint": ("MediaTek TV SoC. Основная ценность — boot-лог (ошибки "
                    "DDR/eMMC/AVB) и сервисное меню пульта."),
    },
    {
        "id": "hisilicon_tv",
        "name": "HiSilicon TV (Hi3751 — Huawei/Honor, Kivi/Haier)",
        "prompt_signature": None,
        "markers": ["Hi3751", "Hisilicon", "HISI", "fastbootd"],
        "weight": 8,
        "intercept_key": "Enter при включении; часть плат требует test-point для fastboot",
        "commands": ("help, printenv/setenv/saveenv, mmc, bootm/booti, reset — "
                     "мало документировано, начинать с help."),
        "forbidden": None,
        "ai_hint": "HiSilicon TV: UART часто только boot-лог (fastboot). Диагностика по логу.",
    },
    {
        "id": "novatek_tv",
        "name": "Novatek TV (NT72671/NT72676)",
        "prompt_signature": None,
        "markers": ["NT72671", "NT72676", "Novatek", "NTK"],
        "weight": 8,
        "intercept_key": "Enter при включении (если не заблокировано)",
        "commands": "help, printenv/setenv/saveenv, reset.",
        "forbidden": "Консоль часто только на чтение лога.",
        "ai_hint": "Novatek: обычно только boot-лог.",
    },
    {
        "id": "generic_uboot",
        "name": "Generic U-Boot (fallback)",
        "prompt_signature": None,
        "markers": ["Hit any key to stop autoboot", "U-Boot 20"],
        "weight": 7,
        "intercept_key": "любая клавиша при 'Hit any key to stop autoboot'",
        "commands": ("help, printenv/setenv/saveenv, bdinfo, version, mmc info, "
                     "fatls/fatload, tftpboot, md/mm/mw/cp/cmp/crc32, "
                     "bootm/booti, go, reset."),
        "forbidden": "Сначала выполнить 'help' и 'bdinfo', затем только команды из вывода help.",
        "ai_hint": "Стандартный U-Boot, вендор неизвестен.",
    },
]

_window = ""
_active = None


def reset():
    global _window, _active
    _window = ""
    _active = None


def feed(text):
    """Принять кусок вывода терминала и пересчитать активный профиль."""
    global _window, _active
    if not text:
        return
    _window = (_window + text)[-WINDOW_SIZE:]
    _active = _score()


def current():
    return _active


def _match_prompt():
    tail = _window.rsplit("\n", 1)[-1]
    if not tail.strip():
        return None
    for p in PROFILES:
        sig = p.get("prompt_signature")
        if sig and sig in tail:
            return p
    return None


def _score():
    prompt_hit = _match_prompt()
    best, best_score = None, 0
    for p in PROFILES:
        score = sum(p["weight"] for m in p["markers"] if m in _window)
        if score > best_score:
            best, best_score = p, score
    if best_score >= MIN_SCORE or prompt_hit:
        return best or prompt_hit
    return None


def prompt_hint():
    """Блок для добавления в system prompt запроса к ИИ."""
    p = _active
    if not p:
        return ""
    return (
        "[BOOTLOADER PROFILE: %s]\n"
        "Консоль: %s\n"
        "Вход в консоль: %s\n"
        "Разрешённые команды: %s\n"
        "Запрещено: %s\n"
        "Инструкция: %s\n" % (
            p["name"],
            p.get("prompt_signature") or "(нет prompt, определён по boot-логу)",
            p["intercept_key"],
            p["commands"],
            p.get("forbidden") or "shell-синтаксис Linux недоступен",
            p["ai_hint"],
        )
    )


def intercept_hint():
    return _active["intercept_key"] if _active else ""
