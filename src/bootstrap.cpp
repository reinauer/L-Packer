//--------------------------------------------------------------------
//		L-Packer
//		Amiga & Atari executable packer
//		Written by Arnaud Carré aka Leonard/Oxygene (@leonard_coder)
//		https://github.com/arnaud-carre/L-Packer
//--------------------------------------------------------------------
				
#include <stdio.h>
#include <string.h>		// memcmp
#include <vector>
#include "bootstrap.h"
#include "bootstrap_data_gen.h"

static const Bootstrap sDataBootstrap =
{
	eAtari, kLz4, true, false, nullptr, 3 * 4		// dummy kLz4 and 12 bytes header for data mode
};

// bootstrap_builder and its assembly sources are not part of this checkout.
// Wrap the generated Amiga stubs without changing their depackers. Save the
// CLI length (D0) and pointer (A0) before text output or decompression, then
// restore them after CacheClearU, with the original entry stack intact.
static const uint8_t sAmigaEntry[] =
{
	0x48, 0xe7, 0x80, 0x80, // movem.l d0/a0,-(sp)
	0x60, 0x0a,             // bra.s original_bootstrap
	0x58, 0x8f,             // addq.l #4,sp (discard second hunk entry)
	0x22, 0x5f,             // movea.l (sp)+,a1 (first hunk entry)
	0x4c, 0xdf, 0x01, 0x01, // movem.l (sp)+,d0/a0
	0x4e, 0xd1,             // jmp (a1)
};

// Assembled from amiga_library.asm. The library initializer must preserve
// Exec's callee-saved registers and keep the complete packed segment list.
static const uint8_t sAmigaLibraryEntry[] =
{
	0x43, 0xfa, 0xff, 0xfa, // lea entry-4(pc),a1
	0x20, 0x08,             // move.l a0,d0
	0xe5, 0x88,             // lsl.l #2,d0
	0xb3, 0xc0,             // cmpa.l d0,a1
	0x67, 0x04,             // beq.s initialize
	0x70, 0xff, 0x4e, 0x75, // moveq #-1,d0; rts
	0x48, 0xe7, 0x3f, 0x3e, // movem.l d2-d7/a2-a6,-(sp)
	0x2f, 0x08,             // move.l a0,-(sp)
	0x60, 0x00, 0x00, 0x1e, // bra.w depacker
	0x22, 0x6f, 0x00, 0x04, // movea.l 4(sp),a1 (patched slot)
	0xd3, 0xfc, 0x12, 0x34, 0x56, 0x78, // adda.l #Resident offset,a1
	0x50, 0x8f,             // addq.l #8,sp
	0x22, 0x1f,             // move.l (sp)+,d1
	0x2c, 0x78, 0x00, 0x04, // movea.l 4.w,a6
	0x4e, 0xae, 0xff, 0x9a, // jsr InitResident(a6)
	0x4c, 0xdf, 0x7c, 0xfc, // movem.l (sp)+,d2-d7/a2-a6
	0x4e, 0x75,             // rts
};

static void WriteWord(uint8_t* data, int value)
{
	data[0] = uint16_t(value) >> 8;
	data[1] = uint16_t(value) & 255;
}

void PatchAmigaLibraryBootstrap(uint8_t* data, int residentSlot, uint32_t residentOffset)
{
	WriteWord(data + 28, residentSlot == 0 ? 4 : 0);
	WriteWord(data + 32, residentOffset >> 16);
	WriteWord(data + 34, residentOffset & 65535);
}

static bool WrapAmigaBootstrap(const Bootstrap& source, std::vector<uint8_t>& data,
	bool library = false)
{
	// These offsets describe the common loader, before the algorithm-specific
	// depacker. Validate the instructions so regenerated stubs fail safely.
	const int start = source.text ? 20 : 0;
	const int reloc = start + (source.algo == kZx0 ? 42 : 44);
	const int exit = start + (source.algo == kZx0 ? 118 : 120);
	const uint8_t tail[] = {0x58, 0x4f, 0x4e, 0x75, 'L', 'P', 'K', '!'};
	const uint8_t* code = source.data;
	if (source.size < uint32_t(exit + sizeof(tail)) ||
		code[start] != 0x4d || code[start + 1] != 0xfa ||
		code[reloc] != 0x4b || code[reloc + 1] != 0xfa ||
		(code[start + 2] * 256 + code[start + 3]) != (65536 - start - 2) ||
		(code[reloc + 2] * 256 + code[reloc + 3]) != (65536 - reloc - 2) ||
		memcmp(code + exit, tail, sizeof(tail)) != 0)
		return false;

	const uint8_t* entry = library ? sAmigaLibraryEntry : sAmigaEntry;
	const int entrySize = library ? sizeof(sAmigaLibraryEntry) : sizeof(sAmigaEntry);
	const int finish = library ? 26 : 6;
	data.assign(entry, entry + entrySize);
	data.insert(data.end(), code, code + source.size);
	uint8_t* body = data.data() + entrySize;
	// Both LEAs must still find the segment link immediately before the new
	// entry. All other PC-relative references, including text, move together.
	WriteWord(body + start + 2, -entrySize - start - 2);
	WriteWord(body + reloc + 2, -entrySize - reloc - 2);
	WriteWord(body + exit, 0x6000); // bra.w to the restore code
	WriteWord(body + exit + 2, finish - entrySize - exit - 2);
	return true;
}

static const int kBootstrapCount = sizeof(sBootstraps) / sizeof(sBootstraps[0]);

struct BootstrapTable
{
	Bootstrap entries[kBootstrapCount];
	std::vector<uint8_t> data[kBootstrapCount];

	BootstrapTable(bool library = false)
	{
		for (int i = 0; i < kBootstrapCount; i++)
		{
			entries[i] = sBootstraps[i];
			if (entries[i].platform != eAmiga)
				continue;
			if (!WrapAmigaBootstrap(sBootstraps[i], data[i], library))
			{
				entries[i].data = nullptr;
				continue;
			}
			entries[i].data = data[i].data();
			entries[i].size = uint32_t(data[i].size());
		}
	}
};

const Bootstrap* GetRightBootstrap(Platform_e platform, ePackingAlgorithm_t algo,
	bool flash, bool text, bool library)
{
	if (library && (platform != eAmiga || flash || text))
		return nullptr;
	if (Platform_e::eData == platform)
		return &sDataBootstrap;

	static const BootstrapTable table;
	static const BootstrapTable libraryTable(true);
	const Bootstrap* it = library ? libraryTable.entries : table.entries;
	for (int i=0;i<kBootstrapCount;i++)
	{
		if ((it->platform == platform) &&
			(it->algo == algo) &&
			(it->flash == flash) &&
			(it->text == text))
		{
			if (it->data == nullptr)
			{
				printf("ERROR: Unsupported Amiga bootstrap layout\n");
				return nullptr;
			}
			return it;
		}
		it++;
	}
	printf("ERROR: No bootstrap found!\n");
	return nullptr;
}

bool IsLPackerExecutable(const uint8_t* data, int size, Platform_e platform)
{
	if (platform == eAmiga)
	{
		// The Resident slot and offset are patched per library. Recognize the
		// fixed prefix without comparing those operands.
		if (size >= int(sizeof(sAmigaLibraryEntry)) &&
			memcmp(data, sAmigaLibraryEntry, 26) == 0 &&
			memcmp(data + 36, sAmigaLibraryEntry + 36,
				sizeof(sAmigaLibraryEntry) - 36) == 0)
			return true;
		// Recognize both old stubs and wrapped stubs, including -text output.
		// Skip the text prefix because its length placeholder is patched.
		for (int i = 0; i < kBootstrapCount; i++)
		{
			const Bootstrap& original = sBootstraps[i];
			if (original.platform != eAmiga)
				continue;
			const int offset = original.text ? 20 : 0;
			if (size >= offset + 28 &&
				memcmp(data + offset, original.data + offset, 28) == 0)
				return true;
			const Bootstrap* wrapped = GetRightBootstrap(eAmiga, original.algo,
				original.flash, original.text);
			const int wrappedOffset = int(sizeof(sAmigaEntry)) + offset;
			if (wrapped && size >= wrappedOffset + 28 &&
				memcmp(data, sAmigaEntry, sizeof(sAmigaEntry)) == 0 &&
				memcmp(data + wrappedOffset, wrapped->data + wrappedOffset, 28) == 0)
				return true;
		}
		return false;
	}
	struct HeaderCheck
	{
		int offset;
		int size;
	};

	static const HeaderCheck hc[eMaxPlatform] =
	{
		{ 8, 6 * 2 },
		{ 0, 14 * 2 },
		{0,0},
	};

	const HeaderCheck& h = hc[platform];
	if (size > h.offset + h.size)
	{
		const Bootstrap* bs = GetRightBootstrap(platform, kZx0, false, false);
		if (0 == memcmp(bs->data+h.offset, data+h.offset, h.size))
			return true;
	}
	return false;
}
