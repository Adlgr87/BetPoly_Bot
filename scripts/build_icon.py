#!/usr/bin/env python3
"""Regenera `web/static/icon.png` dentro del presupuesto de la Mini App (R-06).

El icono original era un PNG de 1024×1024 y 921 KB que Telegram sirve tal cual
en un hueco de ~300 px: el activo más pesado del proyecto con diferencia. Este
script lo deja en 512×512 con una paleta de 64 colores (suficiente para un
degradado como este) y por debajo de 80 KB.

Pillow **no** es dependencia del bot: solo se importa aquí, y si falta el script
sale con código 2 y una instrucción clara. Los tests del presupuesto no
necesitan Pillow.

Uso:
    python scripts/build_icon.py                    # usa web/static/icon.png
    python scripts/build_icon.py ruta/al/original.png
    python scripts/build_icon.py --check            # solo verifica el resultado
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "web" / "static" / "icon.png"
SIZE = 512
COLORS = 64
#: Tope del plan (§9): el icono por debajo de 80 KB.
BUDGET_BYTES = 80 * 1024


def _pillow() -> Any | None:
    """Devuelve ``PIL.Image`` o ``None`` si Pillow no está instalado."""
    try:
        from PIL import Image  # noqa: PLC0415 - dependencia opcional
    except ImportError:
        return None
    return Image


def build(source: Path, target: Any = TARGET, image_module: Any = None) -> int:
    """Cuantiza `source` en `target`; devuelve el código de salida."""
    module = _pillow() if image_module is None else image_module
    if module is None:
        print(
            "❌ Falta Pillow. Instálalo solo para generar el activo:\n"
            "   pip install pillow",
            file=sys.stderr,
        )
        return 2
    if not source.exists():
        print(f"❌ No existe {source}", file=sys.stderr)
        return 2

    image = module.open(source).convert("RGB")
    resized = image.resize((SIZE, SIZE), module.LANCZOS)
    optimized = resized.quantize(colors=COLORS, method=module.MEDIANCUT)
    optimized.save(target, optimize=True)
    return check(target)


def check(target: Any = TARGET, image_module: Any = None) -> int:
    """Verifica tamaño y dimensiones sin volver a escribir el archivo."""
    size = Path(target).stat().st_size
    print(f"{Path(target)}: {size:,} B ({size / 1024:.1f} KB)")
    if size > BUDGET_BYTES:
        print(
            f"❌ Supera el presupuesto de {BUDGET_BYTES / 1024:.0f} KB. "
            "Baja COLORS o usa WebP.",
            file=sys.stderr,
        )
        return 1
    module = _pillow() if image_module is None else image_module
    if module is None:  # sin Pillow no se pueden leer las dimensiones
        return 0
    with module.open(target) as image:
        width, height = image.size
    if (width, height) != (SIZE, SIZE):
        print(f"❌ Se esperaba {SIZE}×{SIZE} y hay {width}×{height}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    if "--check" in argv:
        return check()
    source = Path(argv[1]) if len(argv) > 1 else TARGET
    return build(source)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
