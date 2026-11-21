"""The two purges of SPEC section 4. The callers only come here after the archive copy is written and checked."""
import os
import re
import subprocess

RECORD_VAR = re.compile(r"HwErrRec[0-9A-Fa-f]{4}-414e6bdd-e47b-47cc-b244-bb61020cf516")


def delete_variable(efivars, name):
    """Delete one error-record variable: remove the immutable attribute with the chattr program, then the file.

    The path is built here and the name checked here, so that no caller can ever have another file deleted."""
    if not RECORD_VAR.fullmatch(name):
        raise ValueError(f"{name} is not an error-record variable")
    path = os.path.join(efivars, name)
    try:  # a failure of chattr itself is ignored: the deletion says whether it mattered
        subprocess.run(["chattr", "-i", path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except OSError:
        pass
    os.unlink(path)


def remove_folder(path):
    """Remove a staging folder, a migrated legacy folder, or a run folder already renamed for its removal (SPEC 4):
    its files first, then its subfolders in the order they are listed, then the folder itself. The first failure
    stops it. A symbolic link is removed, never followed."""
    if os.path.islink(path):
        raise OSError(f"{path} is a symbolic link")
    folders = []
    with os.scandir(path) as entries:
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                folders.append(e.path)
            else:
                os.unlink(e.path)
    for folder in folders:
        remove_folder(folder)
    os.rmdir(path)
