import io

from PIL import Image


def dhash(data: bytes, size: int = 8) -> str:
    """差异哈希（64 位，十六进制）。同一张图不同尺寸/轻微压缩，哈希距离很小。"""
    img = Image.open(io.BytesIO(data)).convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(img.tobytes())
    bits = 0
    for row in range(size):
        for col in range(size):
            bits = (bits << 1) | (px[row * (size + 1) + col] > px[row * (size + 1) + col + 1])
    return f"{bits:016x}"


def distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def is_duplicate(h: str, known: list[str], threshold: int = 5) -> bool:
    return any(distance(h, k) <= threshold for k in known)
