"""Moving files without copying them: one copy per song on the music disk.

A rename or hard link only works within one mount (separate bind mounts of the same disk
count as different mounts), so each helper falls back to copying where it cannot.
"""
import errno
import os
import shutil

CANNOT = (errno.EXDEV, errno.EROFS, errno.EACCES, errno.EPERM, errno.EMLINK)


def move(source, target):
    """Move a file; across mounts this copies and deletes the source."""
    try:
        os.replace(source, target)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        shutil.move(str(source), str(target))


def take(source, target):
    """Move a file here, or copy it when the source cannot be moved (another mount, read-only)."""
    try:
        os.replace(source, target)
    except OSError as error:
        if error.errno not in CANNOT:
            raise
        shutil.copy2(source, target)


def link(source, target):
    """A second name for the same file, or a copy where a link is impossible."""
    try:
        os.link(source, target)
    except OSError as error:
        if error.errno not in CANNOT:
            raise
        shutil.copy2(source, target)
