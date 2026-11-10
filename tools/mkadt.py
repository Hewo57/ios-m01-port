#!/usr/bin/env python3
"""Build the minimal Apple Device Tree that the SWEET xnu needs to boot.

Only what the early boot path actually reads (see NOTES.md); every MMIO
address comes from the sweet live FDT. Usage:  mkadt.py OUT.adt [--dump] [--qemu] [--highmem]
"""
import hashlib, os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adt  # noqa: E402

# Kernel-managed RAM: above every Qualcomm XPU carve-out (see NOTES.md)
PHYS_BASE = 0xb0000000
MEM_SIZE = 0x4d000000
# Panic log buffer, carved from the TOP of the dram range and exposed to XNU as
# /pram + /chosen/embedded-panic-log-size. Without it, pe_init.c's
# check_for_panic_log() leaves gPanicBase == 0, debug_log_init() bails out twice
# and the kernel hangs right after "kdp_core zlib memory" — reproduced on QEMU
# and independently on ios-fajita.
PANIC_LOG_SIZE = 0x100000
# --highmem (phone only): the whole upper DDR bank. The live FDT memory node is
# <0x80000000 2G>, <0x1_00000000 0xfcc00000>; every fixed reserved-memory
# carve-out is in the low bank and the dynamic pools are limited below 4G.
HIGH_PHYS_BASE = 0x100000000
HIGH_MEM_SIZE = 0xfcc00000
# /arm-io children are relative to this; it must not be 0 (serial_init bails)
SOC_BASE = 0x01000000

GICD, GICD_SIZE = 0x17a00000, 0x10000
GICR, GICR_SIZE = 0x17a60000, 0x100000
FB, FB_SIZE = 0x9c000000, 0x1700000
SWEET_ECID = 0x5357454554000001  # 'SWEET' + 1, synthetic
FB_W, FB_H, FB_STRIDE = 1080, 2400, 1080 * 4
# Internal UFS host (ufshc@1d84000 "ufs_mem"); QEMU virt has none
UFS, UFS_SIZE = 0x01d84000, 0x3000
TIMEBASE = 19200000
VTIMER_INTID = 19
# QEMU virt (-M virt,gic-version=3 -cpu cortex-a76): GICD/GICR and the generic timer's CNTFRQ
QEMU_GICD, QEMU_GICR, QEMU_TIMEBASE = 0x08000000, 0x080a0000, 62500000
QEMU_VTIMER_INTID = 27

# ---- Samsung Galaxy M01 (SM-M015G/DS, SDM439) ------------------------------
# Kernel-managed RAM: above every fixed/dynamic Qualcomm and Samsung carve-out
# in the low bank (sec_debug 0x9F000000, ss_plog, ramoops 0x9FB00000, splash
# 0x90000000, modem/adsp/wcnss below 0x8D000000, dynamic pools below 0x90000000).
# 0xA0000000 is 32MB aligned and leaves 1.5GB. The shim/live-FDT path may
# override this with --dram-base/--dram-size.
M01_PHYS_BASE = 0xA0000000
M01_MEM_SIZE = 0x60000000
# GICv2: distributor + CPU interface (qcom,msm-qgic2 in the real M01 DTB).
M01_GICD, M01_GICD_SIZE = 0x0b000000, 0x1000
M01_GICC, M01_GICC_SIZE = 0x0b002000, 0x1000
# Continuous-splash framebuffer left by ABL: 720x1520, 20MiB.
M01_FB, M01_FB_SIZE = 0x90000000, 0x1400000
M01_FB_W, M01_FB_H = 720, 1520
# eMMC (sdhc_1) and microSD (sdhc_2), from the M01 DTB.
M01_SDHC1, M01_SDHC1_SIZE = 0x07824900, 0x500
M01_SDHC2, M01_SDHC2_SIZE = 0x07864900, 0x500
M01_TIMEBASE = 19200000
# msm8937 timer PPIs: <1 2> secure phys, <1 3> phys, <1 4> virt, <1 1> hyp.
# XNU uses CNTV, so the virtual timer is PPI 4 => INTID 16 + 4 = 20.
M01_VTIMER_INTID = 20
# Physical MPIDRs, in DT order: little cluster 0x0..0x3, big 0x100..0x103.
M01_MPIDRS = [0x0, 0x1, 0x2, 0x3, 0x100, 0x101, 0x102, 0x103]

BOOT_MANIFEST_HASH = hashlib.sha384(b'sweet boot manifest').digest()

# The iOS userland is the iPhone X (iPhone10,3 / D22AP) IPSW. MobileGestalt
# derives product type, device class, home-button-less UI, artwork etc. from
# the root identity and /product of the real D22AP device tree. That tree is
# an Apple artifact (kept out of git): extract it from the IPSW with
#   tools/remotezip.py URL DeviceTree.d22ap.im4p DIR
#   ipsw img4 im4p extract DIR/Firmware/all_flash/DeviceTree.d22ap.im4p -o d22ap.adt
D22AP_ADT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../research/ipsw-x/d22ap.adt')


def d22ap_product(path=D22AP_ADT):
    """/product properties of the real D22AP tree, without the syscfg
    placeholders iBoot would fill in (flag bit set) and the node bookkeeping."""
    if not os.path.exists(path):
        print(f'warning: {path} missing, no /product node', file=sys.stderr)
        return None
    root, _ = adt.parse(open(path, 'rb').read())
    for c in root.children:
        if dict((k, v) for k, f, v in c.props).get('name') == b'product\0':
            return [(k, v) for k, f, v in c.props if not f and k not in ('name', 'AAPL,phandle')]
    raise SystemExit(f'{path}: no /product node')

u32 = lambda *v: struct.pack('<%dI' % len(v), *v)
u64 = lambda *v: struct.pack('<%dQ' % len(v), *v)
cstr = lambda s: s.encode() + b'\0'


def node(name, props=(), children=()):
    n = adt.Node()
    n.props = [('name', 0, cstr(name))] + [(k, 0, v) for k, v in props]
    n.children = list(children)
    return n


def build(seed=None, qemu=False, highmem=False, m01=False,
          dram_base=None, dram_size=None):
    """qemu=True: same tree, but with QEMU virt's GIC addresses and CNTFRQ (tools/qemu-sweet.py).
    m01=True: Samsung Galaxy M01 / SDM439 board (GICv2, SDHCI, 720x1520)."""
    if m01:
        phys_base = M01_PHYS_BASE if dram_base is None else dram_base
        mem_size = M01_MEM_SIZE if dram_size is None else dram_size
    else:
        phys_base, mem_size = (HIGH_PHYS_BASE, HIGH_MEM_SIZE) if highmem else (PHYS_BASE, MEM_SIZE)
    # Carve the panic log off the top so the kernel never allocates over it.
    panic_base = phys_base + mem_size - PANIC_LOG_SIZE
    mem_size -= PANIC_LOG_SIZE
    if m01:
        gicd, gicr, timebase = M01_GICD, M01_GICC, M01_TIMEBASE
    else:
        gicd, gicr, timebase = (QEMU_GICD, QEMU_GICR, QEMU_TIMEBASE) if qemu else (GICD, GICR, TIMEBASE)
    fb, fb_size = (M01_FB, M01_FB_SIZE) if m01 else (FB, FB_SIZE)
    fb_w, fb_h, fb_stride = (M01_FB_W, M01_FB_H, M01_FB_W * 4) if m01 else (FB_W, FB_H, FB_STRIDE)
    vtimer = M01_VTIMER_INTID if m01 else (QEMU_VTIMER_INTID if qemu else VTIMER_INTID)
    seed = seed if seed is not None else os.urandom(64)
    phandle = iter(range(1, 1000))
    ph = lambda: ('AAPL,phandle', u32(next(phandle)))

    chosen = node('chosen', [
        ph(),
        ('dram-base', u64(phys_base)),
        ('dram-size', u64(mem_size)),
        ('random-seed', seed),
        ('debug-enabled', u32(1)),
        ('embedded-panic-log-size', u32(PANIC_LOG_SIZE)),
        ('firmware-version', cstr('sweet-shim')),
        ('system-firmware-version', cstr('sweet-shim')),
        # iBoot's SHA-384 of the boot manifest; /sbin/mount -P 1 exits unless
        # it exists (bind mounts /private/preboot/<hex>/usr/standalone/firmware,
        # tolerating a missing directory). Fixed stand-in value.
        ('boot-manifest-hash', BOOT_MANIFEST_HASH),
        # libimg4 (cryptexd) aborts "failed to get crypto algorithm" without
        # it; real A11+ device trees say sha2-384.
        ('crypto-hash-method', cstr('sha2-384')),
        # MobileGestalt's screen class: display-scale (u32, default 0) comes
        # from /chosen, the pixel size from IOMobileFramebuffer. Scale 0 makes
        # SpringBoard's FBSDisplayMode "infxinf@0x" invalid. 1080x2400 @3x =
        # 360x800 points (like iPhone X class devices).
        ('display-scale', u32(3)),
        # XNU derives IOPlatformUUID (gethostuuid) from these on arm; without
        # them CFPreferences cannot use kCFPreferencesCurrentHost (no ByHost
        # files), so e.g. DataMigration never records LastSystemVersion and
        # migrates on every boot. Synthetic ECID (not the phone's); chip-id
        # of the A11 that the iPhone X userland expects.
        ('unique-chip-id', struct.pack('<Q', SWEET_ECID)),
        ('chip-id', u32(0x8015)),
        # Security status as on a dev-fused, undemoted board (tuple 0,0,1,1):
        # MobileGestalt reads these from /chosen, and mobileactivationd's
        # DeviceType then takes its native "Hactivation" branch (state
        # Activated without an activation record; no SEP, no baseband here).
        ('certificate-production-status', u32(0)),
        ('effective-production-status-ap', u32(0)),
        ('certificate-security-mode', u32(1)),
        ('effective-security-mode-sep', u32(1)),
    ], [node('memory-map', [ph()]),
        # Local boot policy (kern_csr.c): SIP relaxed like `csrutil disable`
        # (lp-sip0: untrusted kexts, unrestricted FS, task_for_pid, kernel
        # debugger, dtrace, nvram), lp-sip1: unauthenticated (unsealed) root.
        # The iOS shared cache is in no trust cache, so dyld can only map it
        # with CSR_ALLOW_UNRESTRICTED_FS (vm_unix.c shared region checks).
        node('asmb', [ph(), ('lp-sip0', u64(0x6f)), ('lp-sip1', u32(1))])])

    # SWEET SMP: all eight cores in physical-core order (0-5 Cortex-A55,
    # 6-7 Cortex-A76), one homogeneous cluster. The kernel defaults to one
    # and uses cpus=N to select an initial prefix (N <= 8).
    # QEMU retains its original single-CPU tree: its MPIDRs differ from sweet.
    # M01: 8x Cortex-A53, physical MPIDRs 0x0..0x3 (little) and 0x100..0x103
    # (big); clock frequencies are the SDM439 nominal values.
    cpu_nodes = []
    if m01:
        mpidrs = M01_MPIDRS
    else:
        mpidrs = [core << 8 for core in range(1 if qemu else 8)]
    for core, mpidr in enumerate(mpidrs):
        if m01:
            compat = cstr('arm,cortex-a53') + cstr('ARM,v8')
            clock = 1950000000 if mpidr >= 0x100 else 1450000000
        else:
            compat = cstr('qcom,kryo470') + cstr('ARM,v8')
            clock = 2304000000 if core >= 6 else 1804800000
        cpu_nodes.append(node(f'cpu{core}', [
            ph(),
            ('device_type', cstr('cpu')),
            ('compatible', compat),
            ('reg', u32(mpidr)),  # actual MPIDR Aff1:Aff0, NOT logical ID
            ('cpu-id', u32(core)),   # descriptive: XNU assigns IDs by DT order
            ('cluster-id', u32(0 if not m01 else (1 if mpidr >= 0x100 else 0))),
            ('cluster-type', u32(0)),  # homogeneous CLUSTER_TYPE_SMP, not AMP
            ('die-cluster-id', u32(0)),
            ('cluster-core-id', u32(core)),
            ('l2-cache-id', u32(core)),  # distinct per-core L2 nodes in live FDT
            ('l3-cache-id', u32(0)),     # shared L3
            ('state', cstr('running' if core == 0 else 'stopped')),
            ('timebase-frequency', u32(timebase)),
            ('clock-frequency', u32(clock)),
            ('bus-frequency', u32(100000000)),
            ('memory-frequency', u32(1866000000)),
            ('peripheral-frequency', u32(timebase)),
            ('fixed-frequency', u32(timebase)),
        ]))
    cpus = node('cpus', [ph(), ('#address-cells', u32(1)),
                         ('#size-cells', u32(0))], cpu_nodes)

    if m01:
        # GICv2: distributor then CPU interface. The XNU GICv2 driver added by
        # 0083-m01-gicv2.patch parses these two regions in order.
        gic = node('gic', [
            ph(),
            ('compatible', cstr('arm,gic')),
            ('interrupt-controller', b''),
            ('virtual-timer-intid', u32(vtimer)),
            ('reg', u64(gicd - SOC_BASE, M01_GICD_SIZE,
                        gicr - SOC_BASE, M01_GICC_SIZE)),
        ])
    else:
        gic = node('gic', [
            ph(),
            ('compatible', cstr('arm,gic-v3')),
            ('interrupt-controller', b''),
            ('virtual-timer-intid', u32(vtimer)),
            ('reg', u64(gicd - SOC_BASE, GICD_SIZE, gicr - SOC_BASE, GICR_SIZE)),
        ])
    arm_io = node('arm-io', [
        ph(),
        ('device_type', cstr('sweet-io')),
        ('compatible', cstr('sweet-io') + cstr('arm-io')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
        ('ranges', u64(0, SOC_BASE, 0x100000000 - SOC_BASE)),
    ], [gic,
        # No hardware: watchdogd checks IODeviceTree:/arm-io/wdt for no-pmu and
        # otherwise crashes trying to set the PMU boot stage (customer builds
        # ignore the -wdt_no_pmu boot-arg).
        node('wdt', [ph(), ('no-pmu', u32(1))]),
        # Main display: IOMobileFramebuffer.framework counts /arm-io children
        # with this device_type and reads the canvas size from the node
        # (the presented iPhone X size, see SweetFramebuffer.cpp);
        # the kernel's AppleCLCD matches the compatible string.
        node('disp0', [
            ph(),
            ('device_type', cstr('display-subsystem')),
            ('compatible', cstr('sweet,display')),
            ('canvas-width', u32(720 if m01 else 1125)),
            ('canvas-height', u32(1520 if m01 else 2436)),
        ])])

    fbcon = node('sweet-fbcon', [
        ph(),
        ('reg', u64(fb, fb_size)),
        ('width', u32(fb_w)),
        ('height', u32(fb_h)),
        ('stride', u32(fb_stride)),
    ])

    # bsd/dev/sweet_ufs.c: the UFS host ABL leaves running (absolute physical reg).
    ufs = node('sweet-ufs', [
        ph(),
        ('compatible', cstr('qcom,ufshc')),
        ('reg', u64(UFS, UFS_SIZE)),
    ])

    # M01 storage: eMMC (sdhc_1) and microSD (sdhc_2). The XNU SDHCI driver
    # (0085-m01-sdhci.patch) matches these nodes. Kept separate from sweet-ufs.
    sdhci = [
        node('sdhc1', [ph(), ('compatible', cstr('qcom,sdhci-msm')),
                       ('reg', u64(M01_SDHC1, M01_SDHC1_SIZE)),
                       ('bus-width', u32(8)), ('non-removable', u32(1))]),
        node('sdhc2', [ph(), ('compatible', cstr('qcom,sdhci-msm')),
                       ('reg', u64(M01_SDHC2, M01_SDHC2_SIZE)),
                       ('bus-width', u32(4)), ('non-removable', u32(0))]),
    ]

    # pmap_compute_max_asids requires the node even when no overrides are set.
    # An absent pmap-max-asids property selects XNU's own MAX_ASIDS default.
    # No effaceable storage: keybagd then reads /private/var/keybags/systembag.kb
    # as a plain plist instead of decrypting it with the effaceable key.
    defaults = node('defaults', [ph(), ('no-effaceable-storage', u32(1))])

    # MobileGestalt HomeButtonType = IODeviceTree:/buttons home-button-type
    # (D22AP: 2, no Home button; absent = 0 = Home-button device).
    buttons = node('buttons', [
        ph(),
        ('button-names', cstr('volup') + cstr('voldown') + cstr('hold')),
        ('home-button-type', u32(2)),
        ('opposed-power-vol-buttons', u32(1)),
    ])

    product_props = d22ap_product()
    product = [node('product', [ph()] + product_props)] if product_props else []

    # Panic log region. /pram reg = <phys size>; it sits just above the
    # dram-size we expose, so XNU maps it but never allocates into it.
    pram = node('pram', [ph(), ('reg', u64(panic_base) + u64(PANIC_LOG_SIZE))])

    # Identity of the iPhone X whose userland runs here: compatible[0] is
    # hw.model/hw.target (D22AP), model is hw.machine/hw.product
    # (iPhone10,3). 'xiaomi,sweet' stays for SweetPlatformExpert matching;
    # the M01 tree adds samsung,m01 / qcom,sdm439 for board-specific drivers.
    if m01:
        root_compat = (cstr('D22AP') + cstr('iPhone10,3') + cstr('xiaomi,sweet') +
                       cstr('samsung,m01') + cstr('qcom,sdm439') + cstr('AppleARM'))
        storage = sdhci
    else:
        root_compat = (cstr('D22AP') + cstr('iPhone10,3') + cstr('xiaomi,sweet') +
                       cstr('qcom,sm7150') + cstr('AppleARM'))
        storage = [] if qemu else [ufs]
    return node('device-tree', [
        ph(),
        ('compatible', root_compat),
        ('model', cstr('iPhone10,3')),
        ('target-type', cstr('D22')),
        ('target-sub-type', cstr('D22AP')),
        ('manufacturer', cstr('Apple Inc.')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
    ], [chosen, defaults, cpus, arm_io, fbcon, buttons, pram] + product + storage)


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('out')
    ap.add_argument('--dump', action='store_true')
    ap.add_argument('--qemu', action='store_true')
    ap.add_argument('--highmem', action='store_true')
    ap.add_argument('--m01', action='store_true', help='Samsung Galaxy M01 / SDM439')
    ap.add_argument('--dram-base', type=lambda s: int(s, 0),
                    help='override kernel-managed RAM base (M01 live FDT)')
    ap.add_argument('--dram-size', type=lambda s: int(s, 0),
                    help='override kernel-managed RAM size (M01 live FDT)')
    a = ap.parse_args()
    root = build(qemu=a.qemu, highmem=a.highmem, m01=a.m01,
                 dram_base=a.dram_base, dram_size=a.dram_size)
    blob = adt.serialize(root)
    open(a.out, 'wb').write(blob)
    if a.dump:
        adt.dump(adt.parse(blob)[0])
    print(f'{a.out}: {len(blob)} bytes', file=sys.stderr)
