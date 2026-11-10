# ios-m01-port

**Porting Apple's iOS 16 / XNU kernel to the Samsung Galaxy M01 (SM-M015G/DS, Qualcomm SDM439).**

This project ports Apple's open-source XNU kernel (xnu-8796.141.3, the iOS 16 generation) to the Samsung Galaxy M01: SDM439 (Snapdragon 439), 8× Cortex-A53 (ARMv8.0), **GICv2**, eMMC/microSD (SDHCI), **4 KiB pages only**, Adreno 505, WCNSS Wi-Fi.

> **Status: Pre-alpha.** Kernel bring-up in progress. Not booting yet. No GPU, no Wi-Fi, no cellular. This is an engineering port, not a usable phone OS.

## Target hardware

| Component | Specification |
|-----------|---------------|
| SoC | Qualcomm SDM439 (Snapdragon 439), 8× Cortex-A53 @ 1.95/1.45 GHz |
| RAM | 3 GB LPDDR3 (SKU-dependent) |
| Storage | 32 GB eMMC (SDHCI, 8-bit) + dedicated microSD slot (SDHCI, 4-bit) |
| Display | 5.7" 720×1520 HD+ (MDSS/MDP5, DSI) |
| GPU | Adreno 505 (no GPMU) |
| Wi-Fi | Integrated WCNSS (Pronto, SMD) |
| Cellular | Not supported in this port |
| Bootloader | Samsung ABL (Odin/Heimdall only, no fastboot boot) |
| Page size | 4 KiB (A53 does not support 16 KiB granules) |
| Interrupts | GICv2 (distributor + CPU interface at 0x0B000000/0x0B002000) |

## Current status (2026-11-10)

| Milestone | State |
|-----------|-------|
| **M1 — Shim runs** | ✅ Shim builds, Android boot.img v2 format correct |
| **M2 — XNU runs** | ⏳ CI build running (patches 0082–0089) |
| **M3 — iOS userland** | ❌ Blocked by B1 (4K kernel vs 16K userland) |

### Patches applied (on top of sweet's 81 patches)

| # | Patch | Purpose |
|---|-------|---------|
| 0082 | `m01-board-config.patch` | MACHINE_CONFIG=M01, ARM64_BOARD_CONFIG_M01 + SWEET, M01.h, Cortex-A53 |
| 0083 | `m01-gicv2.patch` | GICv2 driver (MMIO GICD/GICC, Group 1 NS, PPI/SGI, EOI) |
| 0084 | `m01-4k-pages.patch` | Force PAGE_SHIFT_CONST=12, LL/SC instead of CASA |
| 0085 | `m01-framebuffer.patch` | 720×1520 @ 0x90000000, stride 2880 |
| 0086 | `m01-sdhci-driver.patch` + `m01-sdhci-config.patch` | SDHCI driver for eMMC/microSD |
| 0087 | `m01-blsp-i2c.patch` | BLSP I2C controller (i2c_3 at 0x78B6000) |
| 0088 | `m01-ft5446.patch` | FT5446 touch at 0x38, polled 100Hz, HID |
| 0089 | `m01-touch-config.patch` | files.arm64 config for blsp_i2c + ft5446 |

## Major blockers

| Blocker | Status |
|---------|--------|
| **B1: iOS 16 userland on 4K pages** | Unproven — dyld cache format supports 4K, simulator works, production cache untested |
| **B2: GICv2 driver** | Written (0083), not yet compiled |
| **B3: SDHCI driver** | Written (0086), not yet compiled |
| **B4: Samsung Odin-only flash** | No fastboot; every test requires Odin/Heimdall flash |
| **B5: 16K assumptions audit** | Partially done (0084 + 4K shim), needs compile-time verification |
| **B9: Memory map placeholder** | DTB `/memory` is `<0 0 0 0>`; must read live FDT |
| **B10: Bootloader unlock / Knox** | Requires OEM unlock; flashing trips Knox irreversibly |

See [`KNOWN_BLOCKERS.md`](KNOWN_BLOCKERS.md) for full details.

## Repository layout

| Path | What |
|------|------|
| `shim/` | Bare-metal boot shim (C + asm) → Android boot.img v2 |
| `xnu/patches/` | XNU patch series (0001–0081 from sweet + 0082–0089 M01) |
| `.github/workflows/xnu.yml` | Kernel CI build on macos-13 (Intel) |
| `tools/mkadt.py` | Generates Apple Device Tree (GICv2, FB, timer, MPIDRs, SDHCI) |
| `tools/mkpayload.py` | Packs kernel + ADT + boot-args into shim payload |
| `tools/unpack_bootv2.py` | Extracts DTB from stock boot.img |

## How to build (once CI passes)

### Prerequisites
- Samsung Galaxy M01 (SM-M015G/DS) with **OEM unlock enabled**
- **Full partition backups** (persist, modemst1/2, fsg, fsc, boot, dtbo, vbmeta)
- Linux PC with `heimdall` or Windows with Odin
- GitHub account for CI kernel build

### 1. Build kernel (GitHub Actions)
```bash
gh workflow run xnu.yml -R Hewo57/ios-m01-port -f machine_config=M01
gh run download RUN_ID -R Hewo57/ios-m01-port -n xnu-kernel -D xnu/out
```

### 2. Prepare device tree (from live FDT on device)
```bash
# On device (adb shell as root):
cat /proc/device-tree > /sdcard/m01.dtb
# Pull to PC:
adb pull /sdcard/m01.dtb
```

### 3. Build boot image
```bash
python3 tools/mkadt.py shim/out/m01.adt --m01 --dram-base 0xA0000000 --dram-size 0x60000000
python3 tools/mkpayload.py xnu/out/kernel.development.M01 shim/out/m01.adt shim/out/payload.bin \
    --page-size 0x1000 \
    --cmdline 'debug=0x144 serial=3 cpus=1 keepsyms=1 -noprogress rd=sdhci0'
shim/build.sh shim/out/payload.bin      # -> shim/out/shim-boot-m01.img
```

### 4. Flash recovery only (non-destructive test)
```bash
# Boot into download mode (Power + Vol Down + Home)
heimdall flash --RECOVERY shim/out/shim-boot-m01.img
# Device boots to shim framebuffer console
```

### 5. Flash boot + root (requires explicit approval)
```bash
# Requires SDHCI driver working + HFS+ root on microSD
heimdall flash --BOOT shim/out/shim-boot-m01.img
```

## QEMU testing (no hardware)
```bash
# Kernel + GICv2 + 4K MMU + PSCI
qemu-system-aarch64 -machine virt,gic-version=2 -cpu cortex-a53 -m 2G \
  -kernel xnu/out/kernel.development.M01 \
  -append 'debug=0x144 serial=3 cpus=1 keepsyms=1 -noprogress'
```

## Credits

Built on Apple's open-source releases (xnu, libpthread, hfs, IOHIDFamily), [BearSSL](https://bearssl.org), [blacktop/darwin-xnu-build](https://github.com/blacktop/darwin-xnu-build), sweet/ios-sailfish reference ports, Samsung/Qualcomm published kernel sources, and [TWRP](https://twrp.me). iOS itself is Apple's and is **not distributed here**.

## Documentation

| File | Purpose |
|------|---------|
| `FEASIBILITY_REPORT.md` | Technical feasibility (7 questions answered) |
| `KNOWN_BLOCKERS.md` | Ranked blockers with evidence |
| `HARDWARE_ANALYSIS.md` | Verified vs assumed hardware facts |
| `COMPATIBILITY.md` | Sweet/sailfish → M01 patch mapping |
| `DEVELOPMENT_LOG.md` | Step-by-step engineering log |
| `PROJECT_STATUS.md` | Current state table |
| `BUILD_INSTRUCTIONS.md` | Build commands and safety gates |
| `NOTES.md` | Handoff notes for next agent |

---

**No Apple firmware or calibration data in this repo. No partition table, bootloader, modem, recovery, or calibration modifications without explicit human approval.**