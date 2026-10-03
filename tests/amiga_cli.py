#!/usr/bin/env python3
"""Execute packed Amiga stubs on a 68000 (requires pip install machine68k).

Usage: python3 tests/amiga_cli.py build/L-Packer
No ROMs or Amiga OS installation are required; OS calls are stubbed.
"""

import argparse
import itertools
from pathlib import Path
import struct
import subprocess
import tempfile

from machine68k import CPUType, Machine, Register


def longs(*values):
    return struct.pack(">" + "I" * len(values), *values)


def fixture():
    # Record incoming D0, A0 and SP, then return 37 to the caller.
    code = bytes.fromhex("23c00000080023c80000080423cf0000080870254e75")
    code += b"CLI preservation test data! " * 32
    code += bytes(-len(code) % 4)
    code += longs(0, 0)
    chip = b"CHIP" + longs(0)
    image = longs(1011, 0, 2, 0, 1, len(code) // 4, (1 << 30) | 256)
    image += longs(1001, len(code) // 4) + code
    image += longs(1004, 1, 0, len(code) - 8, 1, 1, len(code) - 4, 0, 1010)
    image += longs(1002, len(chip) // 4) + chip
    image += longs(1004, 1, 0, 4, 0, 1010)
    return image, code, chip


def load_packed(mem, image):
    pos = 0

    def read_long():
        nonlocal pos
        value = struct.unpack_from(">I", image, pos)[0]
        pos += 4
        return value

    assert read_long() == 1011
    assert read_long() == 0
    count = read_long()
    assert count == 3
    assert read_long() == 0 and read_long() == count - 1
    sizes = [(read_long() & 0x3fffffff) * 4 for _ in range(count)]
    # Segment links are BPTRs; the code starts four bytes after each link.
    links = [0x10000, 0x20000, 0x30000]
    for i, (link, size) in enumerate(zip(links, sizes)):
        mem.w32(link, links[i + 1] // 4 if i + 1 < count else 0)
        mem.clear_block(link + 4, size, 0xA5)
        kind = read_long()
        if kind == 1010:
            kind = read_long()
        assert kind == 1001
        length = read_long() * 4
        assert length <= size
        mem.w_block(link + 4, image[pos:pos + length])
        pos += length
    assert read_long() == 1010 and pos == len(image)
    return [link + 4 for link in links], sizes


def execute(image, code, chip, version, arguments, text):
    machine = Machine(CPUType.M68000, 16 * 1024)
    try:
        cpu, mem = machine.cpu, machine.mem
        entries, sizes = load_packed(mem, image)
        end = machine.create_execute_end("returned")
        tid = machine.traps.alloc(lambda opcode, pc: end)
        mem.w16(0x7000, 0xA000 | tid)
        mem.w32(4, 0x4000)
        mem.w16(0x4000 + 20, version)
        # CacheClearU deliberately clobbers both CLI registers.
        mem.w_block(0x4000 - 636, bytes.fromhex("70ff91c84e75"))
        printed = []

        def output(opcode, pc):
            address = cpu.r_reg(Register.D1)
            length = cpu.r_reg(Register.D2)
            printed.append(bytes(mem.r_block(address, length)))
            cpu.w_reg(Register.D0, 0xDEADBEEF)
            cpu.w_reg(Register.A0, 0)

        tid = machine.traps.alloc(output)
        mem.w16(0x5200, 0xA000 | tid)
        mem.w16(0x5202, 0x4E75)
        mem.w32(0x5000 + 172, 0x5400)
        cpu.w_sr(0)
        cpu.w_reg(Register.A2, 0x5000)
        cpu.w_reg(Register.A5, 0x5200)
        arg_ptr = 0x6000 if arguments is not None else 0
        arg_data = arguments or b""
        if arg_ptr:
            mem.w_block(arg_ptr, arg_data + b"\0")
        cpu.w_reg(Register.D0, len(arg_data))
        cpu.w_reg(Register.A0, arg_ptr)
        sp = 0xF0000
        mem.w32(sp, 0x7000)
        cpu.w_sp(sp)
        cpu.w_pc(entries[0])
        result = machine.execute(2000000)
        assert result.result is end, "packed program did not return"
        assert mem.r32(0x800) == len(arg_data), "D0 was not preserved"
        assert mem.r32(0x804) == arg_ptr, "A0 was not preserved"
        assert mem.r32(0x808) == sp, "entry stack changed"
        assert cpu.r_sp() == sp + 4, "return stack changed"
        assert cpu.r_reg(Register.D0) == 37, "return code changed"
        relocated_code = code[:-8] + longs(entries[1], entries[2])
        relocated_chip = chip[:-4] + longs(entries[1])
        assert bytes(mem.r_block(entries[1], len(code))) == relocated_code
        assert bytes(mem.r_block(entries[2], len(chip))) == relocated_chip
        assert bytes(mem.r_block(entries[2] + len(chip),
                                 sizes[2] - len(chip))) == bytes(sizes[2] - len(chip))
        assert printed == ([text] if text else [])
    finally:
        machine.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packer", type=Path)
    args = parser.parse_args()
    packer = args.packer.resolve()
    image, code, chip = fixture()
    count = 0
    with tempfile.TemporaryDirectory(prefix="lpacker-cli-") as directory:
        root = Path(directory)
        source = root / "input"
        source.write_bytes(image)
        message = root / "message"
        message.write_bytes(b"Loading...\n")
        for algo, flash, text in itertools.product(
                ("zx0", "lz4", "deflate"), (False, True), (False, True)):
            packed = root / "packed"
            command = [str(packer), str(source), str(packed), "-" + algo]
            if flash:
                command += ["-flash", "-hwreg", "180"]
            if text:
                command += ["-text", str(message)]
            subprocess.run(command, check=True, capture_output=True)
            for version, arguments in itertools.product(
                    (34, 37), (b"SCSI FULL\n", b"", None)):
                execute(packed.read_bytes(), code, chip, version, arguments,
                        message.read_bytes() if text else b"")
                count += 1
            # Repacking must recognize the new bootstrap, including -text.
            result = subprocess.run(
                [str(packer), str(packed), str(root / "repacked"), "-" + algo],
                capture_output=True, text=True)
            assert "already packed" in result.stdout, result.stdout
    print(f"PASS: {count} Amiga startup cases; all 12 variants reject repacking")


if __name__ == "__main__":
    main()
