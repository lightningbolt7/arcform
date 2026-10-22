"""Registry entries for the Arcform right-click menu.

Everything lives under HKEY_CURRENT_USER, so no admin rights are needed.
"""

import sys
import winreg
from pathlib import Path

from converters import CONVERSIONS, FORMAT_NAMES, copy_targets
from shrink import SHRINKABLE, SIZES

APP_DIR = Path(__file__).resolve().parent
SCRIPT = APP_DIR / "arcform.py"
ICON = APP_DIR / "arcform.ico"

FILE_MENU = r"Software\Classes\SystemFileAssociations\{ext}\shell\Arcform"
# Right-clicking empty space in a folder or on the desktop.
BACKGROUND_MENU = r"Software\Classes\Directory\Background\shell\Arcform"

# Submenu keys are shown in name order: conversions, Copy as, Shrink, then merge items.
COPY_MENU = "20_copy"
SHRINK_MENU = "30_shrink"
MERGE_ADD = "50_merge_add"
MERGE_GENERATE = "51_merge_generate"
MERGE_VIEW = "52_merge_view"
MERGE_CLEAR = "53_merge_clear"
SEPARATOR_BEFORE = 0x20


def pythonw_path() -> str:
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return str(windowless if windowless.exists() else exe)


def command(*args: str) -> str:
    return f'"{pythonw_path()}" "{SCRIPT}" ' + " ".join(args)


def set_value(path: str, name: str, value) -> None:
    kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, name, 0, kind, value)


def delete_tree(path: str) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                delete_tree(f"{path}\\{child}")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass


def add_submenu(path: str, label: str = "Arcform", icon: bool = True, separator: bool = False) -> None:
    set_value(path, "MUIVerb", label)
    set_value(path, "SubCommands", "")  # tells Explorer this entry is a submenu
    if separator:
        set_value(path, "CommandFlags", SEPARATOR_BEFORE)
    if icon and ICON.exists():
        set_value(path, "Icon", str(ICON))


def add_item(base: str, key: str, label: str, cmd: str, separator: bool = False) -> None:
    item = f"{base}\\shell\\{key}"
    set_value(item, "MUIVerb", label)
    if separator:
        set_value(item, "CommandFlags", SEPARATOR_BEFORE)
    set_value(f"{item}\\command", "", cmd)


def register_file_menus() -> None:
    for ext, targets in CONVERSIONS.items():
        base = FILE_MENU.format(ext=ext)
        delete_tree(base)  # start clean on reinstall
        add_submenu(base)
        for i, target in enumerate(targets):
            # Prefix with a number so Explorer keeps our order.
            add_item(base, f"{i:02d}_{target}", f"Convert to {FORMAT_NAMES[target]}",
                     command("convert", target, '"%1"'))

        copies = copy_targets(ext)
        if copies:
            sub = f"{base}\\shell\\{COPY_MENU}"
            add_submenu(sub, "Copy as", icon=False, separator=True)
            for i, target in enumerate(copies):
                add_item(sub, f"{i:02d}_{target}", FORMAT_NAMES[target], command("copy", target, '"%1"'))

        if ext in SHRINKABLE:
            sub = f"{base}\\shell\\{SHRINK_MENU}"
            add_submenu(sub, "Shrink to fit", icon=False)
            for i, (size, label) in enumerate(SIZES.items()):
                add_item(sub, f"{i:02d}_{size}mb", label, command("shrink", str(size), '"%1"'))

    add_item(FILE_MENU.format(ext=".pdf"), MERGE_ADD, "Add to merge queue",
             command("merge-add", '"%1"'), separator=True)


def refresh_merge_menu(count: int) -> None:
    """Show "Generate merge (N PDFs)" while the queue has files, hide it when empty."""
    label = f"Generate merge ({count} PDF{'' if count == 1 else 's'})"
    for ext in CONVERSIONS:
        base = FILE_MENU.format(ext=ext)
        for key in (MERGE_GENERATE, MERGE_VIEW, MERGE_CLEAR):
            delete_tree(f"{base}\\shell\\{key}")
        if count:
            # .pdf already has a separator above "Add to merge queue".
            add_item(base, MERGE_GENERATE, label, command("merge-generate", '"%1"'),
                     separator=ext != ".pdf")
            add_item(base, MERGE_VIEW, "View merge queue", command("merge-view", '"%1"'))
            add_item(base, MERGE_CLEAR, "Clear merge queue", command("merge-clear"))

    delete_tree(BACKGROUND_MENU)
    if count:
        add_submenu(BACKGROUND_MENU)
        add_item(BACKGROUND_MENU, MERGE_GENERATE, label, command("merge-generate", '"%V"'))
        add_item(BACKGROUND_MENU, MERGE_VIEW, "View merge queue", command("merge-view", '"%V"'))
        add_item(BACKGROUND_MENU, MERGE_CLEAR, "Clear merge queue", command("merge-clear"))


def unregister() -> None:
    for ext in CONVERSIONS:
        delete_tree(FILE_MENU.format(ext=ext))
    delete_tree(BACKGROUND_MENU)
