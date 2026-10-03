#!/usr/bin/env python3
"""Run library bootstraps on a 68000, with modeled Exec/RAMLIB services.

Usage: python3 tests/amiga_library.py build/L-Packer
Requires machine68k. This is not a full AmigaOS or hardware test.
"""

import argparse
import itertools
from pathlib import Path
import struct
import subprocess
import tempfile

from machine68k import CPUType, Machine, Register
from amiga_cli import load_packed, longs


def fixture(autoinit=True, chip=False, failure=False, invalid=None):
    # Ordinary executable entry must not run: it records an error and returns.
    code = bytes.fromhex("21fc00000001080c70ff4e75")
    code += bytes(-len(code) % 4)
    init_offset = len(code)
    # Record incoming D0/A0/A6, retain segList in the library base, and return it.
    init = bytes.fromhex("21c0080421c8080021ce0808227c0000900023480024")
    if failure:
        init += bytes.fromhex("70004e75")
    else:
        if not autoinit:
            init += bytes.fromhex("4eaefe74")  # jsr AddLibrary(a6)
        init += bytes.fromhex("203c000090004e75")
    code += init + b"Library compression fixture! " * 32
    code += bytes(-len(code) % 4)

    # Resident in a DATA hunk, testing both a merged offset and a CHIP hunk.
    tag_offset, name_offset, table_offset = 8, 36, 64
    data = bytearray(80)
    data[tag_offset:tag_offset + 26] = (
        bytes.fromhex("4afc") + longs(tag_offset, len(data)) +
        bytes([0x80 if autoinit else 0, 42, 9, 0]) +
        longs(name_offset, name_offset, table_offset if autoinit else init_offset))
    data[name_offset:name_offset + 13] = b"test.library\0"
    # A modeled MakeLibrary consumes this table before calling the init hook.
    data[table_offset:table_offset + 16] = longs(64, 0, 0, init_offset)
    relocs = {1: [tag_offset + 2, tag_offset + 6, tag_offset + 14,
                  tag_offset + 18]}
    if autoinit:
        relocs[1].append(tag_offset + 22)
        relocs[0] = [table_offset + 12]
    else:
        relocs[0] = [tag_offset + 22]
    if invalid == "missing":
        data[tag_offset:tag_offset + 2] = b"\0\0"
    elif invalid == "self":
        relocs[1].remove(tag_offset + 2)
    elif invalid == "device":
        data[tag_offset + 12] = 3
    elif invalid == "name":
        data[tag_offset + 14:tag_offset + 18] = longs(len(data))
    elif invalid == "init":
        data[tag_offset + 22:tag_offset + 26] = longs(0xfffffffe)
    elif invalid == "multiple":
        other = len(data)
        data += data[tag_offset:tag_offset + 26]
        data[other + 2:other + 6] = longs(other)
        relocs[1].append(other + 2)
        data += bytes(-len(data) % 4)
    flag = (1 << 30) if chip else 0
    image = longs(1011, 0, 3, 0, 2, len(code) // 4,
                  flag | (len(data) // 4), flag | 32)
    image += longs(1001, len(code) // 4) + code + longs(1010)
    image += longs(1002, len(data) // 4) + data + longs(1004)
    for target, offsets in relocs.items():
        image += longs(len(offsets), target, *offsets)
    image += longs(0, 1010, 1003, 32, 1010)
    return image, len(code), tag_offset, name_offset, table_offset, init_offset, len(data)


def resident_tags(mem, entries, sizes):
    """RAMLIB's word-aligned matchword/self-pointer search over loaded hunks."""
    return [address
            for entry, size in zip(entries, sizes)
            for address in range(entry, entry + size - 25, 2)
            if mem.r16(address) == 0x4afc and mem.r32(address + 2) == address]


def execute(image, metadata, autoinit, chip, failure, version, cli=False):
    machine = Machine(CPUType.M68000, 16 * 1024)
    try:
        cpu, mem = machine.cpu, machine.mem
        entries, sizes = load_packed(mem, image)
        segment = (entries[0] - 4) // 4
        links = [mem.r32(entry - 4) for entry in entries]
        # RAMLIB cannot see the compressed Resident; it takes its entry fallback.
        assert not resident_tags(mem, entries, sizes)
        end = machine.create_execute_end("returned")
        tid = machine.traps.alloc(lambda opcode, pc: end)
        mem.w16(0x7000, 0xa000 | tid)
        mem.w32(4, 0x4000)
        mem.w16(0x4000 + 20, version)
        mem.w_block(0x4000 - 636, bytes.fromhex("70ff91c84e75"))
        code_size, tag_offset, name_offset, table_offset, init_offset, data_size = metadata
        data_base = entries[2] if chip else entries[1] + code_size
        resident = data_base + tag_offset
        registered, calls = [], []

        def add_library(opcode, pc):
            registered.append(cpu.r_reg(Register.A1))

        tid = machine.traps.alloc(add_library)
        mem.w16(0x4000 - 396, 0xa000 | tid)
        mem.w16(0x4000 - 394, 0x4e75)

        def init_resident(opcode, pc):
            calls.append(cpu.r_reg(Register.A1))
            assert calls == [resident]
            assert cpu.r_reg(Register.D1) == segment
            assert cpu.r_reg(Register.A6) == 0x4000
            assert mem.r32(resident + 2) == resident
            assert mem.r8(resident + 12) == 9
            assert mem.r32(resident + 14) == data_base + name_offset
            assert mem.r8(resident + 10) == (0x80 if autoinit else 0)
            hook = mem.r32(resident + 22)
            if autoinit:
                assert hook == data_base + table_offset
                assert mem.r32(hook) == 64
                hook = mem.r32(hook + 12)
            assert hook == entries[1] + init_offset
            cpu.w_reg(Register.A1, hook)
            cpu.w_reg(Register.A0, segment)
            cpu.w_reg(Register.D0, 0x9000 if autoinit else 0)

        def finish_init(opcode, pc):
            if autoinit and cpu.r_reg(Register.D0):
                registered.append(cpu.r_reg(Register.D0))

        tid = machine.traps.alloc(init_resident)
        mem.w16(0x4000 - 102, 0xa000 | tid)
        mem.w16(0x4000 - 100, 0x4e91)  # jsr (a1): actual library init code
        tid = machine.traps.alloc(finish_init)
        mem.w16(0x4000 - 98, 0xa000 | tid)
        mem.w16(0x4000 - 96, 0x4e75)
        saved_regs = (Register.D2, Register.D3, Register.D4, Register.D5,
                      Register.D6, Register.D7, Register.A2, Register.A3,
                      Register.A4, Register.A5, Register.A6)
        values = [0x123400 + i * 256 for i in range(len(saved_regs))]
        values[-1] = 0x4000
        cpu.w_sr(0)
        for reg, value in zip(saved_regs, values):
            cpu.w_reg(reg, value)
        cpu.w_reg(Register.A0, 0x6000 if cli else segment)
        cpu.w_reg(Register.D0, 17)
        sp = 0xf0000
        mem.w32(sp, 0x7000)
        cpu.w_sp(sp)
        cpu.w_pc(entries[0])
        result = machine.execute(2000000)
        assert result.result is end, "library bootstrap did not return"
        assert cpu.r_sp() == sp + 4
        assert [cpu.r_reg(reg) for reg in saved_regs] == values
        assert [mem.r32(entry - 4) for entry in entries] == links
        assert mem.r32(0x80c) == 0, "ordinary executable entry was called"
        if cli:
            assert cpu.r_reg(Register.D0) == 0xffffffff
            assert not calls and not registered
            return
        assert calls == [resident]
        assert mem.r32(0x800) == segment
        assert mem.r32(0x804) == (0x9000 if autoinit else 0)
        assert mem.r32(0x808) == 0x4000
        assert cpu.r_reg(Register.D0) == (0 if failure else 0x9000)
        assert registered == ([] if failure else [0x9000])
        # The retained segList must let Expunge/UnLoadSeg reach every allocation.
        unload = segment if failure else mem.r32(0x9000 + 36)
        walked = []
        while unload:
            assert unload not in walked
            walked.append(unload)
            unload = mem.r32(unload * 4)
        assert walked == [(entry - 4) // 4 for entry in entries]
        bss_start = data_base + data_size
        assert bytes(mem.r_block(bss_start, 128)) == bytes(128)
    finally:
        machine.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packer", type=Path)
    args = parser.parse_args()
    packer = str(args.packer.resolve())
    count = 0
    with tempfile.TemporaryDirectory(prefix="lpacker-library-") as directory:
        root = Path(directory)
        source, packed = root / "test.library", root / "packed.library"
        for algo, autoinit, chip, failure in itertools.product(
                ("zx0", "lz4", "deflate"), (False, True), (False, True), (False, True)):
            original, *metadata = fixture(autoinit, chip, failure)
            source.write_bytes(original)
            subprocess.run([packer, str(source), str(packed), "-library", "-" + algo],
                           check=True, capture_output=True)
            image = packed.read_bytes()
            for version in (34, 37):
                execute(image, metadata, autoinit, chip, failure, version)
                count += 1
            execute(image, metadata, autoinit, chip, failure, 37, cli=True)
            count += 1
            for options in ([], ["-library"]):
                result = subprocess.run([packer, str(packed), str(root / "repacked"),
                                         *options], capture_output=True, text=True)
                assert result.returncode != 0 and "already packed" in result.stdout
        # Additional embedded Residents must not change selection of the first.
        original, *metadata = fixture(invalid="multiple")
        source.write_bytes(original)
        subprocess.run([packer, str(source), str(packed), "-library", "-lz4"],
                       check=True, capture_output=True)
        execute(packed.read_bytes(), metadata, True, False, False, 37)
        for invalid in ("missing", "self", "device", "name", "init"):
            source.write_bytes(fixture(invalid=invalid)[0])
            output = root / (invalid + ".library")
            result = subprocess.run([packer, str(source), str(output), "-library"],
                                    capture_output=True, text=True)
            assert result.returncode != 0 and "ERROR:" in result.stdout
            assert not output.exists()
        source.write_bytes(fixture()[0])
        for option in ("-data", "-flash", "-hwreg", "-text"):
            extra = ["180"] if option == "-hwreg" else [str(source)] if option == "-text" else []
            result = subprocess.run([packer, str(source), str(packed), "-library",
                                     option, *extra], capture_output=True, text=True)
            assert result.returncode != 0 and "cannot be combined" in result.stdout
        source.write_bytes(b"not an Amiga library")
        result = subprocess.run([packer, str(source), str(packed), "-library"],
                                capture_output=True, text=True)
        assert result.returncode != 0 and "requires an Amiga" in result.stdout
        source.write_bytes(fixture()[0])
        subprocess.run([packer, str(source), str(packed), "-library", "-target", "4",
                        "-pad"], check=True, capture_output=True)
        assert packed.stat().st_size == 4096
        execute(packed.read_bytes(), fixture()[1:], True, False, False, 37)
    print(f"PASS: {count} library startup cases; validation, repacking and padding")


if __name__ == "__main__":
    main()
