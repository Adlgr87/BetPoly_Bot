#!/usr/bin/env python3
"""Genera ``web/static/icon.png`` dentro del presupuesto de la Mini App (R-06).

El icono original era un PNG de 1024×1024 y 921 KB que Telegram sirve tal cual en
un hueco de ~300 px: el activo más pesado del proyecto con diferencia. Este
script lo reduce a 512×512 con una paleta de 64 colores (suficiente para un
degradado como este) y lo deja por debajo de 80 KB.

Es idempotente: si el archivo ya está en 512×512 se vuelve a cuantizar sin
pérdida visible, así que sirve tanto para regenerar desde el original como para
volver a comprimir uno nuevo.

Uso::

    python scripts/build_icon.py                     # usa web/static/icon.png
    python scripts/build_icon.py ruta/al/original.png
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "web" / "static" / "icon.png"
SIZE = 512
COLORS = 64
#: Presupuesto del plan: el icono por debajo de 80 KB.
BUDGET_BYTES = 80 * 1024


def _pillow() -> object:
    """Pillow no es dependencia del bot: solo se usa para generar el activo."""
    try:
        from PIL import Image
    except ImportError:
        return None
    return Image


def build(source: Path, target: Path = TARGET, image_module: object = None) -> int:
    """Cuantiza ``source`` en ``target`` y verifica el presupuesto.

    ``image_module`` permite probar el guardarraíl sin instalar Pillow: el
    script no puede ser dependencia de test, pero su tope de tamaño sí debe
    estar bajo test (es lo que impide volver a un PNG de 900 KB).
    """
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

    size = target.stat().st_size
    print(f"✅ {target} — {SIZE}×{SIZE}, {COLORS} colores, {size / 1024:.1f} KB")
    if size > BUDGET_BYTES:
        print(
            f"❌ Supera el presupuesto de {BUDGET_BYTES / 1024:.0f} KB. "
            "Baja COLORS o usa WebP.",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str]) -> int:
    source = Path(argv[1]) if len(argv) > 1 else TARGET
    return build(source)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
