#!/bin/bash
# Build the M01 shim and wrap it in a Samsung Android boot.img v2.
#
#   ./build.sh [payload.bin]
#
# The shim is freestanding AArch64. We use clang + ld.lld because the host
# may not have an aarch64-linux-gnu toolchain; llvm-objcopy/readelf/nm cover
# the binutils steps.
set -euo pipefail
cd "$(dirname "$0")"

CC=${CC:-clang}
LD=${LD:-ld.lld}
TARGET=${TARGET:-aarch64-unknown-none}
OBJCOPY=${OBJCOPY:-llvm-objcopy}
READELF=${READELF:-llvm-readelf}
NM=${NM:-llvm-nm}
CFLAGS="-O2 -ffreestanding -nostdlib -fno-pic -mgeneral-regs-only -mstrict-align -fno-stack-protector -Wall -Wextra -target $TARGET"
# Link with ld.lld directly: this clang has no aarch64 linker driver and would
# otherwise hand the objects to the host gcc/collect2.
LDFLAGS="-nostdlib -static -T shim.ld --build-id=none -N"

# Samsung Galaxy M01 boot image v2 parameters (twrp_sdm439 BoardConfigCommon.mk).
BASE=${BASE:-0x80000000}
PAGESIZE=${PAGESIZE:-2048}
KERNEL_OFFSET=${KERNEL_OFFSET:-0x00008000}
RAMDISK_OFFSET=${RAMDISK_OFFSET:-0x02000000}
TAGS_OFFSET=${TAGS_OFFSET:-0x01e00000}
DTB_OFFSET=${DTB_OFFSET:-0x101f00000}
BOARD=${BOARD:-SRPSJ30A003}
DTB=${DTB:-../dtb/m01q.dtb}

if [ ! -f "$DTB" ]; then
	echo "error: M01 DTB not found at $DTB" >&2
	echo "       set DTB=/path/to/m01q.dtb (e.g. the TWRP prebuilt dtb)" >&2
	exit 1
fi

mkdir -p out
OBJS=""
for f in start.S shim.c console.c mmu.c fdt.c loader.c; do
	$CC $CFLAGS -c "$f" -o "out/${f%.*}.o"
	OBJS="$OBJS out/${f%.*}.o"
done

# The image runs wherever ABL puts it with no relocation pass. C objects must
# be fully position independent; only start.o's header image_size (_end,
# linked at 0) may be an absolute address, so it is excluded here.
COBJS=""
for f in shim.c console.c mmu.c fdt.c loader.c; do
	COBJS="$COBJS out/${f%.*}.o"
done
abs=$($READELF -r $COBJS | grep -c ABS64 || true)
[ "$abs" = 0 ] || { echo "error: $abs absolute relocations in C objects" >&2; exit 1; }

$LD $LDFLAGS $OBJS -o out/shim.elf
$OBJCOPY -O binary out/shim.elf out/Image
# ABL copies the file; pad to image_size so .bss/stacks exist as zeroed bytes.
size=$($NM out/shim.elf | awk '$3=="_end"{print strtonum("0x"$1)}')
truncate -s "$size" out/Image

# Ramdisk: an xnu payload (tools/mkpayload.py) if given as $1, else an empty
# newc cpio (ABL dislikes a zero-size ramdisk on some builds) -> diagnostics.
if [ -n "${1:-}" ]; then
	cp "$1" out/ramdisk.bin
else
	(cd out && rm -rf rd && mkdir rd && cd rd && find . | cpio -o -H newc --quiet | gzip -9 > ../ramdisk.bin)
fi

mkbootimg --header_version 2 --kernel out/Image --ramdisk out/ramdisk.bin \
	--dtb "$DTB" --base "$BASE" \
	--kernel_offset "$KERNEL_OFFSET" --ramdisk_offset "$RAMDISK_OFFSET" \
	--tags_offset "$TAGS_OFFSET" --dtb_offset "$DTB_OFFSET" \
	--pagesize "$PAGESIZE" --board "$BOARD" \
	--os_version 10.0.0 --os_patch_level 2020-01 \
	--cmdline "console=null" -o out/shim-boot-m01.img
# Samsung ABL expects this marker at the end of the boot image.
printf 'SEANDROIDENFORCE' >> out/shim-boot-m01.img

# Host render of the console layout.
gcc -O1 -Wall -o out/host_test console.c host_test.c && ./out/host_test
ls -l out/Image out/shim-boot-m01.img
