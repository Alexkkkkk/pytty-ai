# Ремонт ТВ через UART: полная практика (обучение ИИ)

База собрана с форумов: xdaforums (MStar root/backup), dvbpro.ru (восстановление
MStar), televid-sib.org (коннект терминалом, MStar/Realtek), kenotrontv.ru
(dd-бэкап eMMC, платы 338PB801/338EPB803), 4pda, remont-aud.

## Главные принципы (повторять ИИ всегда)

1. **Бэкап ДО любых правок.** Сначала сливаем разделы/дамп, потом меняем.
2. **FAT32 = лимит 4 ГБ на файл** — userdata больше 4 ГБ не сливается одним
   файлом (MStar: пропускать или резать).
3. **USB-порты у ТВ нумеруются**: боковой = usb 0, нижние = usb 1/2/3.
   Команды зависят от порта — сначала `usb reset 0` / `usb start`, потом работа.
4. **Полный дамп поблочно сливать НЕ надо** — на MStar команды вида
   `emmcbin ... ` без разбора и `mmc dd mmc2usb` портят дамп/флешку.
   Только по разделам, с размером из `mmc part`.
5. **Первый запуск после прошивки длинный** — не выключать, смотреть UART-лог
   и индикатор флешки.
6. **Инженерный режим после recovery**: розовый прямоугольник в углу —
   выход через сервис-меню пульта (MStar: Source 2580 → Factory menu →
   Factory reset).

## MStar (Mstar#, M5621#, mt5863# ...)

### Вход в консоль
- Выключить ТВ из розетки, зажать **Enter**, подать питание, держать до prompt.
- Не вышло — перебирать: esc, Tab, Ctrl+C, Enter, Enter+Пробел, Tab+Пробел.
- Пишет `UART BUS OFF` — UART выключен в Android (см. tv_service_kb.md,
  сервис-меню MSTAR FAC MENU → UART BUS On). Без этого терминал молчит.

### Сброс до заводских настроек (bootloop, зависание на логотипе)
```
recovery_wipe_partition cache     # сначала это, reset, проверить
recovery_wipe_partition data      # полный сброс (предупредить пользователя!)
reset
```
Из загруженного Android (если есть ADB):
```
adb shell recovery --wipe_cache
adb shell recovery --wipe_data    # = factory reset, перезагрузится сам
```

### Бэкап разделов eMMC на USB-флешку
```
mmc part                          # таблица разделов: имена и размеры — сохранить!
usb reset 0                       # инициализация флешки (порт 0 = боковой)
emmcbin 0 0 recovery.bin recovery 65536   # слив ОДНОГО раздела
# имя и размер — из mmc part; повторить для boot, bootargs, system, ...
usb stop
```
Ошибка `** Bad Signature on 0:37 ... [do_emmc_mkbin]: check bininfo of part 37
failed` — НЕ критична, дамп нормальный.

### Восстановление разделов из файлов
```
usb reset 0
usb_partial_upgrade_to_emmc recovery2.bin recovery
usb_partial_upgrade_to_emmc recovery2.bin boot    # boot шьём тем же образом!
usb stop
```
Прошивка всей системы с флешки:
```
custar                            # запуск update с USB, идёт 2-5 минут молча
```
(до появления картинки следим по UART и миганию индикатора флешки)

### Восстановление SPI-flash приставок MStar (без программатора)
```
usb reset
fatls usb 0:1
fatload usb 0:1 0x80000000 firmware_bez_4096.bin
spi_wrc 0x80000000 0x0 0x400000
reset
```
(файл прошивки без первых 4096 байт; размер 0x400000 = 4 МБ для 25Q64)

### Замена eMMC на MStar (нет дампа для программатора)
1. Из дампа-донора извлечь разделы **boot1 и boot2** (boot-области eMMC).
2. Записать их на новую eMMC (программатор или in-circuit).
3. `eboot` — загрузка с eMMC: появится лог и консоль Mstar#.
4. Влить родную прошивку с флешки: `custar` (или штатное обновление с USB).

### Unlock / отключение verified boot (для root/кастома)
```
avbab set_device_state 0
setenv devicestate unlock
saveenv
avbab disable-verity              # иначе bootloop
```
После этого ТВ при загрузке покажет "bootloader is unlocked" и сделает wipe —
это нормально.

### Полезные команды MStar
```
du            # ВЫКЛЮЧАЕТ UART — не вводить случайно!
envbin        # слить переменные окружения на USB
env2flash     # восстановить env из файла
help recovery / help upgrade    # подсказки по синтаксису
```

## Realtek (Realtek>)

### Вход
- Пробел в первые секунды после подачи питания (окно после
  'Enter console mode, disable watchdog').

### Смена панели (после замены матрицы — типовая задача!)
```
panel          # или spanel — список панелей с номерами
panel          # ВВОДИТЬ ДВАЖДЫ (почти всегда!)
# далее по подсказке указать номер нужной панели
saveenv
```
### Обновление прошивки
```
upgrade        # обновить kernel/rootfs с USB
upgrade_part   # обновить отдельный раздел (uboot и т.п.)
tftpboot       # загрузка образа по сети (TFTP-сервер на ПК)
reloadenv      # вернуть заводские переменные окружения
setenv / saveenv / printenv
```
### Полный дамп eMMC через Linux-консоль (если ТВ грузится)
```
dd if=/dev/mmcblk0 of=/mnt/usb/backup.img bs=1M status=progress
```
(через UART 115200 это долго — терпение; платы 338PB801/338EPB803 и аналоги)
Восстановление той же dd в обратную сторону: `dd if=/mnt/usb/backup.img
of=/dev/mmcblk0 bs=1M` — потом обязательно проверить, не умирает ли eMMC
(верификация, повторное чтение).

## Amlogic (GXBB/GXL/G12A bootrom, U-Boot)

### Вход
- Любая клавиша при 'Hit Enter key to stop autoboot'.
- Прошивка с SD/USB без консоли: `aml_autoscript` в корень + зажать reset
  (toothpick method) при включении.

### Прошивка разделов
```
amlmmc read  boot 0x01000000 0x0 0x400000     # слить boot в память
amlmmc write 0x01000000 boot 0x0 0x400000     # записать boot из памяти
store read boot 0x01000000 0x400000           # store-вариант (S905/S912)
booti 0x01000000                              # проверить ядро из RAM ДО записи!
bootm 0x01000000                              # для старых uImage
run update                                    # штатное обновление
```
Правило: **сначала загрузить образ в RAM и проверить (booti/bootm), потом
писать во флеш** — так не убьёшь загрузку ошибочным файлом.

## Android TV / приставки: сброс и recovery

### Методы по возрастанию жёсткости
1. **Soft reset**: выдернуть питание на 10-15 сек (не стирает ничего).
2. **Сброс из настроек**: Настройки → System → Reset (ТВ должен грузиться).
3. **Recovery через pinhole**: зажать reset (часто внутри AV-гнезда),
   подать питание, держать 8-15 сек → меню recovery → Wipe data/factory
   reset → Reboot. Навигация: USB-клавиатура стрелками+Enter.
4. **ADB** (если включён отладчик): `adb reboot recovery`, далее wipe в меню.
5. **update.zip**: прошивка с USB из recovery/штатного апдейтера.
6. **UART + загрузчик**: методы выше по платформе (MStar/Realtek/Amlogic).

### Команды ADB для сервиса
```
adb devices                    # проверка соединения
adb shell recovery --wipe_data # полный сброс (bootloop, не грузится GUI)
adb shell recovery --wipe_cache
adb reboot recovery / bootloader
adb pull /storage/emulated/0/Download/file.bin   # забрать файл с устройства
adb push file.bin /storage/emulated/0/Download/  # положить файл на устройство
```

## Закачка/скачивание файлов через UART (все платформы)

| Способ | Команда | Скорость | Когда |
|---|---|---|---|
| TFTP | `tftpboot 0x80000000 file.bin` (сервер на ПК: tftpd32/64) | высокая | сеть есть, файл большой |
| YModem | `loady` + отправка файла из терминала | средняя | нет сети/USB |
| Kermit | `loadb` + kermit-протокол | низкая | только если ymodem нет |
| USB | `fatload usb 0:1 0x80000000 file.bin` | высокая | флешка FAT32 |
| MMC/eMMC | `fatload mmc 0 file.bin 0x80000000` | высокая | SD-карта |

Слить файл С устройства НА флешку (MStar): `emmcbin`, `envbin`.
После fatload обязательно проверять: `crc32 0x80000000 <размер>` против
контрольной суммы оригинала.

## Правка параметров без перепрошивки

- **bootargs** (cmdline ядра): `printenv bootargs` → `setenv bootargs "..."`
  → `saveenv` → `reset`. Типовые правки: консоль UART (console=ttyS0,115200),
  уровень лога (loglevel=7), root=/dev/mmcblk0pX.
- **Переменные MStar**: envbin/env2flash — слить/залить окружение файлом.
- **dtb**: загрузить dtb в RAM, править, записать обратно fatwrite/
  соответствующей командой платформы. Не править dtb, не слив оригинал!
- **Панель/матрица**: Realtek panel/spanel, MStar — через сервис-меню.

## Диагностика: алгоритм ИИ при любом обращении

1. Попросить/проверить **последние строки UART-лога** — где остановилась
   загрузка (bootloader? kernel? Android?).
2. Определить профиль (boot_profiles.py): Realtek/MStar/Amlogic/...
3. Нет лога вообще → UART выключен (MStar UART BUS OFF), неверный порт,
   RX/TX перепутаны, нет питания платы. Тогда: прошивка по USB или дамп
   через программатор.
4. Bootloop на логотипе → wipe cache → wipe data → прошивка.
5. После любой заливки дампа — **верификация** (перечитать и сравнить
   CRC): eMMC может умирать и писаться с ошибками.
