"""Builds ui/assets/app.ico (exe and installer icon) from ui/assets/logo_square.png.

Run once after the logo changes: env/Scripts/python.exe -m installer.make_icon
Each size is stored as a PNG inside the .ico (supported since Windows Vista), so
no imaging library beyond Qt is needed."""
import struct

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage

from config.paths import resource_path

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)  # 20/40: 125 % and 250 % scaling


def _png(image: QImage, size: int) -> bytes:
    scaled = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    scaled.save(buffer, "PNG")
    return bytes(data)


def build_ico(source: QImage) -> bytes:
    images = [_png(source, size) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))  # reserved, type 1 = icon, count
    offset = len(header) + 16 * len(images)
    entries, payload = b"", b""
    for size, png in zip(SIZES, images):
        dim = 0 if size >= 256 else size  # 0 means 256
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        payload += png
    return header + entries + payload


def main() -> None:
    source = QImage(str(resource_path("ui/assets/logo_square.png")))
    if source.isNull():
        raise SystemExit("logo_square.png not found or unreadable")
    target = resource_path("ui/assets/app.ico")
    target.write_bytes(build_ico(source))
    print(f"wrote {target} ({', '.join(map(str, SIZES))} px)")


if __name__ == "__main__":
    main()
