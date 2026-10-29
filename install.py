"""Add (or remove) the "Arcform" entry in the Windows right-click menu.

    python install.py            add the menu
    python install.py uninstall  remove the menu

Entries are written under HKEY_CURRENT_USER, so no admin rights are needed.
"""

import sys

import menu
import merge_queue
from converters import CONVERSIONS, copy_targets
from shrink import SHRINKABLE


ICON_SIZES = [16, 20, 24, 32, 40, 48, 64, 256]  # what Windows asks for at different display scales


def draw_logo(m: int = 2048):
    """The Arcform logo: an arc shaped into an A, with an arrow for the crossbar."""
    from PIL import Image, ImageDraw, ImageFilter, ImageOps

    white = (255, 255, 255, 255)
    # Teal-to-blue tile. The ramp is rotated, so take the middle and stretch it back to full range.
    ramp = Image.linear_gradient("L").resize((2 * m, 2 * m)).rotate(30, resample=Image.Resampling.BICUBIC)
    ramp = ImageOps.autocontrast(ramp.crop((m // 2, m // 2, m * 3 // 2, m * 3 // 2)))
    tile = Image.composite(Image.new("RGBA", (m, m), (37, 99, 235, 255)),
                           Image.new("RGBA", (m, m), (20, 200, 190, 255)), ramp)
    glow = Image.new("L", (m, m), 0)
    ImageDraw.Draw(glow).ellipse((-0.3 * m, -0.75 * m, 1.3 * m, 0.45 * m), fill=38)
    tile = Image.composite(Image.new("RGBA", (m, m), white), tile, glow.filter(ImageFilter.GaussianBlur(m * 0.06)))
    shape = Image.new("L", (m, m), 0)
    ImageDraw.Draw(shape).rounded_rectangle((0.03 * m, 0.03 * m, 0.97 * m, 0.97 * m), radius=0.235 * m, fill=255)
    logo = Image.new("RGBA", (m, m), (0, 0, 0, 0))
    logo.paste(tile, mask=shape)

    # The A: a thick arch on two legs.
    glyph = Image.new("RGBA", (m, m), (0, 0, 0, 0))
    d = ImageDraw.Draw(glyph)
    cx, cy, r, w = m / 2, 0.47 * m, 0.235 * m, 0.115 * m
    d.arc((cx - r - w / 2, cy - r - w / 2, cx + r + w / 2, cy + r + w / 2), 180, 360, fill=white, width=int(w))
    for x in (cx - r, cx + r):
        d.rounded_rectangle((x - w / 2, cy - 2, x + w / 2, 0.80 * m), radius=w / 2, fill=white)
        d.rectangle((x - w / 2, cy - 2, x + w / 2, cy + w), fill=white)
    # The crossbar arrow, its tip stopping short of the right leg.
    y, t = 0.635 * m, 0.06 * m
    tip = cx + r - w / 2 - 0.025 * m
    base = tip - 0.11 * m
    d.rectangle((cx - r, y - t / 2, base + 2, y + t / 2), fill=white)
    d.polygon([(base, y - 0.075 * m), (tip, y), (base, y + 0.075 * m)], fill=white)

    # A soft shadow under the A, then the A itself.
    shadow = Image.new("RGBA", (m, m), (10, 10, 40, 0))
    shadow.putalpha(glyph.getchannel("A").point(lambda v: v * 70 // 255).filter(ImageFilter.GaussianBlur(0.012 * m)))
    logo.alpha_composite(shadow, (0, int(0.012 * m)))
    logo.alpha_composite(glyph)
    return logo


def make_icon() -> None:
    try:
        from PIL import Image
    except ImportError:
        return
    # Drawn big and scaled down, which smooths the edges.
    logo = draw_logo().resize((512, 512), Image.Resampling.LANCZOS)
    logo.save(menu.LOGO)
    logo.resize((256, 256), Image.Resampling.LANCZOS).save(menu.ICON, sizes=[(s, s) for s in ICON_SIZES])


def refresh_explorer() -> None:
    """Tell Explorer the menu changed, so it reloads icons instead of using cached ones."""
    try:
        from win32com.shell import shell, shellcon
    except ImportError:
        return
    shell.SHChangeNotify(shellcon.SHCNE_ASSOCCHANGED, shellcon.SHCNF_IDLIST, None, None)


def install() -> None:
    make_icon()
    menu.register_file_menus()
    menu.refresh_merge_menu(merge_queue.count())
    for ext, targets in CONVERSIONS.items():
        extras = (["copy as"] if copy_targets(ext) else []) + (["shrink"] if ext in SHRINKABLE else [])
        print(f"Added Arcform menu for {ext} -> {', '.join([*targets, *extras])}")
    print("Added PDF merge queue")
    refresh_explorer()
    print("\nDone. Right-click a supported file to see Arcform.")
    print('On Windows 11 it is under "Show more options" (or Shift+Right-click).')


def uninstall() -> None:
    menu.unregister()
    refresh_explorer()
    print("Removed the Arcform right-click menu.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].lower() in ("uninstall", "remove", "--uninstall"):
        uninstall()
    else:
        install()
