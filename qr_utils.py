"""
Self-contained pure-Python QR Code encoder (ISO/IEC 18004, Byte mode, EC level M, Versions 1-10).
Used for offline PDF QR codes and the local /api/qr.svg fallback when CDN JS is unavailable.
"""

from functools import lru_cache
from html import escape

# (version, total_codewords, ec_per_block, [(num_blocks, data_per_block), ...], align_centers, remainder_bits)
_QR_VERSIONS_M = [
    (1, 26, 10, [(1, 16)], [], 0),
    (2, 44, 16, [(1, 28)], [6, 18], 7),
    (3, 70, 26, [(1, 44)], [6, 22], 7),
    (4, 100, 18, [(2, 32)], [6, 26], 7),
    (5, 134, 24, [(2, 43)], [6, 30], 7),
    (6, 172, 16, [(4, 27)], [6, 34], 7),
    (7, 196, 18, [(4, 31)], [6, 22, 38], 0),
    (8, 242, 22, [(2, 38), (2, 39)], [6, 24, 42], 0),
    (9, 292, 22, [(3, 36), (2, 37)], [6, 26, 46], 0),
    (10, 346, 26, [(4, 43), (1, 44)], [6, 28, 50], 0),
]


def _gf_mul(x: int, y: int) -> int:
    r = 0
    for i in range(8):
        if (y >> i) & 1:
            r ^= x << i
    for i in range(14, 7, -1):
        if (r >> i) & 1:
            r ^= 0x11D << (i - 8)
    return r


def _rs_generator(degree: int) -> list:
    poly = [1]
    root = 1
    for _ in range(degree):
        nxt = [0] * (len(poly) + 1)
        for i, c in enumerate(poly):
            nxt[i] ^= c
            nxt[i + 1] ^= _gf_mul(c, root)
        poly = nxt
        root = _gf_mul(root, 2)
    return poly[1:]  # leading 1 omitted


def _rs_remainder(data: list, ec_len: int) -> list:
    gen = _rs_generator(ec_len)
    rem = [0] * ec_len
    for b in data:
        factor = b ^ rem[0]
        rem = rem[1:] + [0]
        for i in range(ec_len):
            rem[i] ^= _gf_mul(gen[i], factor)
    return rem


def _encode_data_codewords(payload: bytes, ver_spec: tuple) -> list:
    version, _, ec_per_blk, block_groups, _, _ = ver_spec
    total_data_cw = sum(n * d for n, d in block_groups)
    cc_bits = 8 if version <= 9 else 16

    bits = []
    def push_bits(val: int, width: int):
        for shift in range(width - 1, -1, -1):
            bits.append((val >> shift) & 1)

    push_bits(0b0100, 4)  # Byte mode indicator
    push_bits(len(payload), cc_bits)
    for byte in payload:
        push_bits(byte, 8)

    cap_bits = total_data_cw * 8
    term_len = min(4, cap_bits - len(bits))
    bits.extend([0] * term_len)
    while len(bits) % 8 != 0:
        bits.append(0)

    cw = []
    for i in range(0, len(bits), 8):
        val = 0
        for b in bits[i:i + 8]:
            val = (val << 1) | b
        cw.append(val)

    pad_bytes = (0xEC, 0x11)
    idx = 0
    while len(cw) < total_data_cw:
        cw.append(pad_bytes[idx % 2])
        idx += 1

    # Split into blocks and compute Reed-Solomon EC
    data_blocks = []
    ec_blocks = []
    pos = 0
    for count, dlen in block_groups:
        for _ in range(count):
            blk = cw[pos:pos + dlen]
            pos += dlen
            data_blocks.append(blk)
            ec_blocks.append(_rs_remainder(blk, ec_per_blk))

    # Interleave data then EC codewords
    interleaved = []
    max_dlen = max(len(b) for b in data_blocks)
    for i in range(max_dlen):
        for blk in data_blocks:
            if i < len(blk):
                interleaved.append(blk[i])
    for i in range(ec_per_blk):
        for blk in ec_blocks:
            interleaved.append(blk[i])
    return interleaved


def _build_function_grid(ver_spec: tuple):
    version, _, _, _, align_centers, _ = ver_spec
    size = 17 + 4 * version
    modules = [[False] * size for _ in range(size)]
    is_func = [[False] * size for _ in range(size)]

    def set_func(r: int, c: int, val: bool):
        modules[r][c] = val
        is_func[r][c] = True

    def draw_finder(top: int, left: int):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = top + dr, left + dc
                if 0 <= r < size and 0 <= c < size:
                    dist = max(abs(dr - 3), abs(dc - 3))
                    dark = dist != 2 and max(dr, dc) < 7 and min(dr, dc) >= 0
                    set_func(r, c, dark)

    draw_finder(0, 0)
    draw_finder(0, size - 7)
    draw_finder(size - 7, 0)

    # Alignment patterns
    for r in align_centers:
        for c in align_centers:
            if (r == 6 and c == 6) or (r == 6 and c == size - 7) or (r == size - 7 and c == 6):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    set_func(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)

    # Timing patterns
    for i in range(8, size - 8):
        val = (i % 2) == 0
        if not is_func[6][i]:
            set_func(6, i, val)
        if not is_func[i][6]:
            set_func(i, 6, val)

    # Reserve format info & dark module
    for i in range(9):
        if i != 6:
            set_func(8, i, False)
            set_func(i, 8, False)
    for i in range(8):
        set_func(8, size - 1 - i, False)
        set_func(size - 1 - i, 8, False)
    set_func(size - 8, 8, True)

    # Version info for v >= 7
    if version >= 7:
        rem = version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        vbits = (version << 12) | rem
        for i in range(18):
            bit = bool((vbits >> i) & 1)
            a, b = size - 11 + (i % 3), i // 3
            set_func(a, b, bit)
            set_func(b, a, bit)

    return modules, is_func


def _draw_format_bits(modules: list, mask: int):
    size = len(modules)
    data = (0b00 << 3) | mask  # EC Level M = 00
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = ((data << 10) | rem) ^ 0x5412

    # ISO 18004: bit 14 (MSB) at (8, 0) / (size-1, 8), bit 0 (LSB) at (0, 8) / (8, size-1)
    for i in range(6):
        modules[8][i] = bool((bits >> (14 - i)) & 1)
    modules[8][7] = bool((bits >> 8) & 1)
    modules[8][8] = bool((bits >> 7) & 1)
    modules[7][8] = bool((bits >> 6) & 1)
    for i in range(9, 15):
        modules[14 - i][8] = bool((bits >> (14 - i)) & 1)

    for i in range(7):
        modules[size - 1 - i][8] = bool((bits >> (14 - i)) & 1)
    for i in range(8):
        modules[8][size - 8 + i] = bool((bits >> (7 - i)) & 1)


def _apply_mask_and_score(base_modules: list, is_func: list, mask: int) -> tuple:
    size = len(base_modules)
    m = [row[:] for row in base_modules]
    for r in range(size):
        for c in range(size):
            if is_func[r][c]:
                continue
            if mask == 0:
                inv = (r + c) % 2 == 0
            elif mask == 1:
                inv = r % 2 == 0
            elif mask == 2:
                inv = c % 3 == 0
            elif mask == 3:
                inv = (r + c) % 3 == 0
            elif mask == 4:
                inv = ((r // 2) + (c // 3)) % 2 == 0
            elif mask == 5:
                inv = ((r * c) % 2) + ((r * c) % 3) == 0
            elif mask == 6:
                inv = (((r * c) % 2) + ((r * c) % 3)) % 2 == 0
            else:
                inv = (((r + c) % 2) + ((r * c) % 3)) % 2 == 0
            if inv:
                m[r][c] = not m[r][c]
    _draw_format_bits(m, mask)

    # Penalty scoring (ISO 18004)
    penalty = 0
    for r in range(size):
        run = 1
        for c in range(1, size):
            if m[r][c] == m[r][c - 1]:
                run += 1
            else:
                if run >= 5:
                    penalty += 3 + (run - 5)
                run = 1
        if run >= 5:
            penalty += 3 + (run - 5)

    for c in range(size):
        run = 1
        for r in range(1, size):
            if m[r][c] == m[r - 1][c]:
                run += 1
            else:
                if run >= 5:
                    penalty += 3 + (run - 5)
                run = 1
        if run >= 5:
            penalty += 3 + (run - 5)

    for r in range(size - 1):
        for c in range(size - 1):
            v = m[r][c]
            if v == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                penalty += 3

    pat1 = [True, False, True, True, True, False, True, False, False, False, False]
    pat2 = [False, False, False, False, True, False, True, True, True, False, True]
    for r in range(size):
        row = m[r]
        for c in range(size - 10):
            seg = row[c:c + 11]
            if seg == pat1 or seg == pat2:
                penalty += 40
    for c in range(size):
        col = [m[r][c] for r in range(size)]
        for r in range(size - 10):
            seg = col[r:r + 11]
            if seg == pat1 or seg == pat2:
                penalty += 40

    dark_count = sum(1 for row in m for cell in row if cell)
    pct_diff = abs((dark_count * 100) / (size * size) - 50.0)
    penalty += int(pct_diff // 5.0) * 10
    return penalty, m


def generate_qr_matrix(text: str, quiet: int = 4) -> list:
    """Return a 2D boolean QR matrix (True = dark module, False = light module)."""
    payload = (text or "").encode("utf-8")
    chosen = None
    for ver in _QR_VERSIONS_M:
        total_data_cw = sum(n * d for n, d in ver[3])
        cc_bits = 8 if ver[0] <= 9 else 16
        needed_bits = 4 + cc_bits + len(payload) * 8
        if needed_bits <= total_data_cw * 8:
            chosen = ver
            break
    if chosen is None:
        raise ValueError(f"QR payload too long ({len(payload)} bytes, max 213)")

    codewords = _encode_data_codewords(payload, chosen)
    modules, is_func = _build_function_grid(chosen)
    size = len(modules)
    all_bits = []
    for cw in codewords:
        for s in range(7, -1, -1):
            all_bits.append((cw >> s) & 1)
    all_bits.extend([0] * chosen[5])

    bit_idx = 0
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for dc in (0, -1):
                c = col + dc
                if not is_func[r][c]:
                    if bit_idx < len(all_bits):
                        modules[r][c] = bool(all_bits[bit_idx])
                        bit_idx += 1
        upward = not upward
        col -= 2

    best_grid = None
    best_score = None
    for mask in range(8):
        score, grid = _apply_mask_and_score(modules, is_func, mask)
        if best_score is None or score < best_score:
            best_score = score
            best_grid = grid

    if quiet > 0:
        full_size = size + 2 * quiet
        padded = [[False] * full_size for _ in range(full_size)]
        for r in range(size):
            padded[r + quiet][quiet:quiet + size] = best_grid[r]
        return padded
    return best_grid


@lru_cache(maxsize=256)
def qr_svg(text: str, cell_size: int = 3, quiet: int = 4) -> str:
    """Render text as a compact standalone SVG QR code (LRU-cached by text and dimensions)."""
    grid = generate_qr_matrix(text, quiet=quiet)
    dim = len(grid) * cell_size
    rects = []
    for r, row in enumerate(grid):
        y = r * cell_size
        for c, dark in enumerate(row):
            if dark:
                rects.append(f'<rect x="{c * cell_size}" y="{y}" width="{cell_size}" height="{cell_size}"/>')
    safe_title = escape(text, quote=True)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" '
        f'width="{dim}" height="{dim}" role="img" aria-label="QR code for {safe_title}">'
        f'<rect width="{dim}" height="{dim}" fill="#ffffff"/>'
        f'<g fill="#000000">{"".join(rects)}</g></svg>'
    )
