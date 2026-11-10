# M01 port — handoff notes

This repository is a fork of **ios-sweet** ported to the **Samsung Galaxy M01
(SM-M015G/DS, Qualcomm SDM439 / Snapdragon 439)**. The upstream sweet project
notes are in `../ios-sweet/NOTES.md`; this file is the M01-specific handoff.

## State

- Branch: `m01` (base: `ios-sweet` at `45263be`, XNU `xnu-8796.141.3`).
- **Shim + ADT build locally.** XNU patches are written and validated to
  apply, but **XNU has not been compiled** (no Apple toolchain here; CI needs
  a GitHub push).
- **No device is connected** (`adb devices` / `lsusb` empty) and **nothing has
  been flashed**.
- SDHCI storage is **not implemented** (Stage D).

## M01 patch series

| Patch | Contents |
| --- | --- |
| `0082-m01-board-config.patch` | `MACHINE_CONFIG=M01`; `ARM64_BOARD_CONFIG_M01` + `_SWEET`; new `pexpert/pexpert/arm64/M01.h`; `arm,cortex-a53` CPU match; CPU name “Cortex-A53”; `-mcpu=cortex-a53` |
| `0083-m01-gicv2.patch` | GICv2 branch in `pexpert/arm/pe_fiq.c`; `GICD`/`GICC` MMIO; Group 1; banked PPI target map; `GICD_SGIR` IPIs; `sleh_irq` dispatch; `ICC_SRE_EL2` guarded with `!M01` |
| `0084-m01-4k-pages.patch` | `arm_init.c` forces `PAGE_SHIFT_CONST=ARM_PGSHIFT`; commpage `casa` → `ldaxr/stxr` LL/SC |

The series applies in order to the sweet baseline. `MACHINE_CONFIG=M01` also
defines `ARM64_BOARD_CONFIG_SWEET`, so the shared sweet drivers compile; M01
deviations are guarded with `#if defined(M01)` / `!defined(M01)`.

## Key constants

| Item | Value |
| --- | --- |
| SoC / CPU | SDM439, 8× Cortex-A53 (ARMv8.0, no LSE/PAN, 4K only) |
| GIC | GICv2, GICD `0x0b000000`, GICC `0x0b002000` |
| Virtual timer | **INTID 20** (msm8937 DT: secure phys 2, phys 3, virt 4, hyp 1) |
| Timebase | 19.2 MHz |
| Framebuffer | `0x90000000`, 720×1520 |
| MPIDRs | `0x0–0x3` (little), `0x100–0x103` (big) |
| eMMC / microSD | SDHCI `0x7824900` / `0x7864900` |
| Kernel DRAM | `0xA0000000`, `0x60000000` (1.5 GB), panic log at top |
| Boot image | v2, pagesize 2048, base `0x80000000`, board `SRPSJ30A003`, `SEANDROIDENFORCE` |

## Build

```sh
# ADT
python3 tools/mkadt.py --m01 /tmp/m01.adt

# payload (4K)
python3 tools/mkpayload.py <xnu-kernel> /tmp/m01.adt /tmp/payload-m01.bin \
    --page-size 0x1000 --cmdline "debug=0x144 serial=3 cpus=1 keepsyms=1 -noprogress"

# shim + Samsung boot image
cd shim
DTB=/tmp/opencode/twrp_sdm439/prebuilt/dtb ./build.sh /tmp/payload-m01.bin
```

Outputs: `shim/out/Image` (shim) and `shim/out/shim-boot-m01.img`.

## CI

`.github/workflows/xnu.yml` defaults to `machine_config=M01`,
`macos_version=13.5`, `kernel_config=DEVELOPMENT`, and watches `main` and
`m01`. Push the branch to GitHub and run the workflow; download the
`xnu-kernel` artifact. Locally there is no `gh` and `origin` is a local path.

## Known gaps / next steps

1. **Compile XNU** (CI) and fix errors.
2. **SDHCI driver** — XNU has no MMC/SDHCI host driver. Write
   `bsd/dev/sweet_sdhci.c` modeled on `bsd/dev/sweet_ufs.c` (polled block
   device), then HFS+ on microSD (non-destructive) or userdata.
3. **Device**: connect the unlocked M01, take the safety backups, and get
   explicit approval before flashing only the recovery partition.
4. **B1**: iOS 16 userland is built for 16K pages; the A53 is 4K-only. This is
   the project-level go/no-go and can only be settled by a real boot.

## Safety

Never modify the partition table, bootloader, modem, persist/efs/fsg/fsc,
recovery or calibration data without explicit human approval. Never flash
automatically. Do not commit Apple firmware or device calibration data.
