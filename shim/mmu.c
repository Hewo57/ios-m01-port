/*
 * M01 (SDM439) EL1 stage-1 MMU with the 4K granule that the Cortex-A53 and
 * the M01 kernel actually support. (The sweet/sailfish shim uses 16K.)
 *
 * T0SZ=28 gives a 36-bit VA whose walk starts at level 1: 1GB blocks at L1,
 * 2MB blocks at L2, 4K pages at L3.
 *
 * Qualcomm XPUs fault on speculative reads of protected carve-outs (TZ,
 * hyp, modem...), so nothing is mapped cacheable except our own image:
 *   - default: Device-nGnRnE, execute-never (same as MMU off)
 *   - the 2MB block(s) holding the image get an L3 table: image pages are
 *     Normal write-back, the rest of the block stays Device
 *   - the splash framebuffer block is Normal non-cacheable (write-combining)
 */
#include <stdint.h>
#include "mmu.h"

#define PG_SHIFT	12
#define PG_SIZE		(1UL << PG_SHIFT)		/* 4K */
#define L2_SHIFT	21
#define L2_SIZE		(1UL << L2_SHIFT)		/* 2MB level-2 block */
#define L1_SHIFT	30
#define L1_SIZE		(1UL << L1_SHIFT)		/* 1GB level-1 block */
#define NENT		512
#define L1_ENTRIES	8				/* first 8GB */
#define L2_MASK		(NENT - 1)

#define ATTR_DEV	0	/* MAIR attr0 = 0x00 Device-nGnRnE */
#define ATTR_WB		1	/* MAIR attr1 = 0xff Normal WB RW-alloc */
#define ATTR_NC		2	/* MAIR attr2 = 0x44 Normal non-cacheable */
#define MAIR_VAL	0x44ff00UL

#define D_BLOCK		1UL
#define D_TABLE		3UL
#define D_PAGE		3UL
#define D_ATTR(i)	((uint64_t)(i) << 2)
#define D_SH_OUTER	(2UL << 8)
#define D_SH_INNER	(3UL << 8)
#define D_AF		(1UL << 10)
#define D_PXN		(1UL << 53)
#define D_UXN		(1UL << 54)

#define DEV_FLAGS	(D_ATTR(ATTR_DEV) | D_AF | D_PXN | D_UXN)
#define WB_FLAGS	(D_ATTR(ATTR_WB) | D_SH_INNER | D_AF | D_UXN)
#define NC_FLAGS	(D_ATTR(ATTR_NC) | D_SH_OUTER | D_AF | D_PXN | D_UXN)

/* TCR: T0SZ=28, IRGN0/ORGN0=WBWA, SH0=inner, TG0=4K, EPD1, TG1=4K, IPS=36 bit */
#define TCR_VAL		(28UL | (1UL << 8) | (1UL << 10) | (3UL << 12) | \
			 (28UL << 16) | (1UL << 23) | (2UL << 30) | (1UL << 32))

#define SCTLR_M		(1UL << 0)
#define SCTLR_A		(1UL << 1)
#define SCTLR_C		(1UL << 2)
#define SCTLR_I		(1UL << 12)
#define SCTLR_WXN	(1UL << 19)

#define rsr(r) ({ uint64_t _v; __asm__ volatile("mrs %0, " r : "=r"(_v)); _v; })
#define wsr(r, v) __asm__ volatile("msr " r ", %0" : : "r"((uint64_t)(v)))

extern char _head[], _end[];

/* 1 L1 + up to 2 L2 (image and framebuffer may be in different 1GB regions)
 * + up to 2 L3 tables, plus slack to align to 4K at runtime. */
static uint64_t pt_pool[6 * NENT] __attribute__((aligned(4096)));

static void dcache_inval_range(uintptr_t start, uintptr_t end)
{
	uint64_t line = 4UL << ((rsr("ctr_el0") >> 16) & 0xf);

	for (uintptr_t a = start & ~(line - 1); a < end; a += line)
		__asm__ volatile("dc ivac, %0" : : "r"(a) : "memory");
	__asm__ volatile("dsb sy" ::: "memory");
}

static void zero_table(uint64_t *t)
{
	for (int i = 0; i < NENT; i++)
		t[i] = 0;
}

int mmu_enable(struct mmu_info *info)
{
	uintptr_t img_lo = (uintptr_t)_head & ~(PG_SIZE - 1);
	uintptr_t img_hi = ((uintptr_t)_end + PG_SIZE - 1) & ~(PG_SIZE - 1);
	uintptr_t pool = ((uintptr_t)pt_pool + PG_SIZE - 1) & ~(PG_SIZE - 1);
	uint64_t *l1 = (uint64_t *)pool;
	uint64_t img_l1 = img_lo >> L1_SHIFT;
	uint64_t fb_l1 = FB_PHYS >> L1_SHIFT;
	uint64_t img_l2_first = img_lo >> L2_SHIFT;
	uint64_t img_l2_last = (img_hi - 1) >> L2_SHIFT;
	uint64_t *l2_img, *l2_fb;
	int n_l2 = 0, n_l3 = 0;

	info->img_lo = img_lo;
	info->img_hi = img_hi;
	info->ttbr = pool;
	if (img_l2_last - img_l2_first > 1)
		return -1;	/* only room for two L3 tables */
	if (img_l1 >= L1_ENTRIES || fb_l1 >= L1_ENTRIES)
		return -1;	/* image/FB outside the identity-mapped first 8GB */

	/* L1: Device everywhere RAM or MMIO can be (first 8GB), rest unmapped */
	zero_table(l1);
	for (uint64_t i = 0; i < L1_ENTRIES; i++)
		l1[i] = (i << L1_SHIFT) | DEV_FLAGS | D_BLOCK;

	/* L2 table for the 1GB region holding the image. */
	l2_img = (uint64_t *)(pool + (uint64_t)(1 + n_l2++) * PG_SIZE);
	zero_table(l2_img);
	l1[img_l1] = (uintptr_t)l2_img | D_TABLE;

	/* L2 table for the framebuffer's 1GB region, if different. */
	if (fb_l1 == img_l1) {
		l2_fb = l2_img;
	} else {
		l2_fb = (uint64_t *)(pool + (uint64_t)(1 + n_l2++) * PG_SIZE);
		zero_table(l2_fb);
		l1[fb_l1] = (uintptr_t)l2_fb | D_TABLE;
		for (int i = 0; i < NENT; i++)
			l2_fb[i] = ((fb_l1 << L1_SHIFT) + ((uint64_t)i << L2_SHIFT)) |
				   DEV_FLAGS | D_BLOCK;
	}

	/* Default every 2MB block in the image region to Device. */
	for (int i = 0; i < NENT; i++)
		l2_img[i] = (((img_l1 << L1_SHIFT) + ((uint64_t)i << L2_SHIFT))) |
			    DEV_FLAGS | D_BLOCK;

	/* L3 for each 2MB block that holds part of the image. */
	for (uint64_t blk = img_l2_first; blk <= img_l2_last; blk++) {
		uint64_t *l3 = (uint64_t *)(pool + (uint64_t)(1 + n_l2 + n_l3) * PG_SIZE);
		uint64_t base = blk << L2_SHIFT;
		n_l3++;
		for (int i = 0; i < NENT; i++) {
			uint64_t pa = base + ((uint64_t)i << PG_SHIFT);
			int ours = pa >= img_lo && pa < img_hi;
			l3[i] = pa | (ours ? WB_FLAGS : DEV_FLAGS) | D_PAGE;
		}
		l2_img[blk & L2_MASK] = (uintptr_t)l3 | D_TABLE;
	}
	info->n_l3 = n_l3;

	/* Framebuffer: Normal non-cacheable, unless the image shares that block. */
	{
		uint64_t fb_l2 = (FB_PHYS >> L2_SHIFT) & L2_MASK;
		uint64_t fb_base = FB_PHYS & ~(L2_SIZE - 1);
		int fb_overlaps_img = fb_base >= (img_l2_first << L2_SHIFT) &&
				      fb_base <= (img_l2_last << L2_SHIFT);
		if (!fb_overlaps_img)
			l2_fb[fb_l2] = fb_base | NC_FLAGS | D_BLOCK;
	}

	/* Image lines may be stale in the cache from ABL; we wrote memory directly. */
	dcache_inval_range(img_lo, img_hi);
	__asm__ volatile("ic iallu; dsb sy; isb" ::: "memory");

	wsr("mair_el1", MAIR_VAL);
	wsr("tcr_el1", TCR_VAL);
	wsr("ttbr0_el1", pool);
	__asm__ volatile("isb; tlbi vmalle1; dsb nsh; isb" ::: "memory");

	uint64_t sctlr = rsr("sctlr_el1");
	info->sctlr_before = sctlr;
	sctlr &= ~(SCTLR_A | SCTLR_WXN);
	sctlr |= SCTLR_M | SCTLR_C | SCTLR_I;
	wsr("sctlr_el1", sctlr);
	__asm__ volatile("isb" ::: "memory");
	info->sctlr_after = rsr("sctlr_el1");
	return 0;
}

/* Translate a VA with AT S1E1R; returns PAR_EL1. */
uint64_t mmu_probe(uintptr_t va)
{
	__asm__ volatile("at s1e1r, %0; isb" : : "r"(va) : "memory");
	return rsr("par_el1");
}
