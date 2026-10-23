#!/usr/bin/env python3
"""Regenera `web/static/icon.png` a partir del logo de marca (R-06).

El logo vive en `web/branding/logo-source.png` (la lámina completa: emblema +
rótulo «BETPOLY BOT»). Telegram solo ofrece un hueco cuadrado de ~300 px para el
avatar y el manifiesto de TON Connect descarga `/static/icon.png`, así que el
script recorta **el emblema** —no el rótulo, que a ese tamaño es ilegible—, lo
encaja centrado sobre el fondo de la propia lámina y lo cuantiza para que quepa
en el presupuesto de 80 KB.

El recorte (`CROP`) es explícito y con test: si alguien cambia la lámina, el
script falla en vez de generar un icono descentrado o recortado.

Pillow **no** es dependencia del bot: solo se importa aquí, y si falta el script
sale con código 2 y una instrucción clara. Los tests del presupuesto no
necesitan Pillow.

Uso:
    python scripts/build_icon.py                    # logo de marca → icon.png
    python scripts/build_icon.py --source otra.png  # otra lámina
    python scripts/build_icon.py --check            # solo verifica el resultado
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
#: Lámina de marca (emblema + rótulo). Se versiona para que el icono sea
#: reproducible; no se sirve al cliente.
SOURCE = ROOT / "web" / "branding" / "logo-source.png"
TARGET = ROOT / "web" / "static" / "icon.png"
#: Emblema dentro de la lámina original (463×480), medido sobre la imagen: el
#: círculo va de x=67 a x=412 y de y=60 a y=312, con la flecha saliendo por
#: arriba a la derecha. El rótulo empieza en y≈320 y queda fuera.
CROP = (58, 0, 426, 322)
SIZE = 512
#: El emblema no es cuadrado: se encaja centrado, sin deformarlo.
MARGIN = 0.04
#: Fondo del icono, tomado de la esquina de la lámina.
BACKGROUND = (22, 33, 47)
#: Ancho (en píxeles del icono) del degradado que difumina el borde del recorte.
FEATHER = 40
COLORS = 128
#: Tope del plan (§9): el icono por debajo de 80 KB.
BUDGET_BYTES = 80 * 1024


def _pillow() -> Any | None:
    """Devuelve ``PIL.Image`` o ``None`` si Pillow no está instalado."""
    try:
        from PIL import Image  # noqa: PLC0415 - dependencia opcional
    except ImportError:
        return None
    return Image


def _feather_mask(module: Any, size: tuple[int, int], width: int) -> Any:
    """Máscara L con el borde degradado, sin depender de `ImageDraw`/`ImageFilter`.

    Se apilan rectángulos concéntricos con alfa creciente; el resultado es un
    degradado de 24 pasos que, sobre un fondo casi uniforme, no deja costura.
    """
    mask = module.new("L", size, 0)
    steps = 24
    for step in range(steps):
        inset = round(width * (1 - step / steps))
        alpha = int(255 * (step + 1) / steps)
        layer = module.new(
            "L", (max(1, size[0] - 2 * inset), max(1, size[1] - 2 * inset)), alpha
        )
        mask.paste(layer, (inset, inset))
    return mask


def build(source: Path | None = None, target: Any = TARGET, image_module: Any = None) -> int:
    """Recorta y cuantiza la lámina `source` en `target`; devuelve el código."""
    module = _pillow() if image_module is None else image_module
    if module is None:
        print(
            "❌ Falta Pillow. Instálalo solo para generar el activo:\n"
            "   pip install pillow",
            file=sys.stderr,
        )
        return 2
    source = SOURCE if source is None else Path(source)
    if not source.exists():
        print(f"❌ No existe {source}", file=sys.stderr)
        return 2

    image = module.open(source).convert("RGB")
    width, height = image.size
    left, top, right, bottom = CROP
    if right > width or bottom > height:  # lámina distinta a la esperada
        print(
            f"❌ El recorte {CROP} no cabe en una lámina de {width}×{height}",
            file=sys.stderr,
        )
        return 2

    emblem = image.crop(CROP)
    box = int(SIZE * (1 - 2 * MARGIN))
    scale = min(box / emblem.size[0], box / emblem.size[1])
    fitted = emblem.resize(
        (max(1, round(emblem.size[0] * scale)), max(1, round(emblem.size[1] * scale))),
        module.LANCZOS,
    )
    canvas = module.new("RGB", (SIZE, SIZE), BACKGROUND)
    mask = _feather_mask(module, fitted.size, FEATHER)
    canvas.paste(fitted, ((SIZE - fitted.size[0]) // 2, (SIZE - fitted.size[1]) // 2), mask)
    optimized = canvas.quantize(colors=COLORS, method=module.MEDIANCUT)
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
    if "--source" in argv:
        index = argv.index("--source")
        try:
            return build(Path(argv[index + 1]))
        except IndexError:
            print("❌ --source necesita una ruta", file=sys.stderr)
            return 2
    return build()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
