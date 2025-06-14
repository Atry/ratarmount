#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import tarfile
from typing import Optional, Tuple

from ratarmountcore.compressions import checkForSplitFile, findAvailableBackend, supportedCompressions
from ratarmountcore.utils import detectRawTar, isRandom

try:
    import fsspec
except ImportError:
    fsspec = None  # type: ignore

try:
    import sqlcipher3
except ImportError:
    sqlcipher3 = None  # type: ignore


def checkInputFileType(path: str, encoding: str = tarfile.ENCODING, printDebug: int = 0) -> Tuple[str, Optional[str]]:
    """Raises an exception if it is not an accepted archive format else returns the real path and compression type."""

    splitURI = path.split('://')
    if len(splitURI) > 1:
        protocol = splitURI[0]
        if fsspec is None:
            raise argparse.ArgumentTypeError("Detected an URI, but fsspec was not found. Try: pip install fsspec.")
        if protocol not in fsspec.available_protocols():
            raise argparse.ArgumentTypeError(
                f"URI: {path} uses an unknown protocol. Protocols known by fsspec are: "
                + ', '.join(fsspec.available_protocols())
            )
        return path, None

    if not os.path.isfile(path):
        raise argparse.ArgumentTypeError(f"File '{path}' is not a file!")
    path = os.path.realpath(path)

    result = checkForSplitFile(path)
    if result:
        return result[0][0], 'part' + result[1]

    with open(path, 'rb') as fileobj:
        fileSize = os.stat(path).st_size

        # Header checks are enough for this step.
        oldOffset = fileobj.tell()
        compression = None
        for compressionId, compressionInfo in supportedCompressions.items():
            try:
                if compressionInfo.checkHeader(fileobj):
                    compression = compressionId
                    break
            finally:
                fileobj.seek(oldOffset)

        try:
            # Determining if there are many frames in zstd is O(1) with is_multiframe
            if compression != 'zst':
                raise Exception()  # early exit because we catch it anyways

            backend = findAvailableBackend(compression)
            if not backend:
                raise Exception()  # early exit because we catch it anyways

            zstdFile = backend.open(fileobj)

            if not zstdFile.is_multiframe() and fileSize > 1024 * 1024:
                print(f"[Warning] The specified file '{path}'")
                print("[Warning] is compressed using zstd but only contains one zstd frame. This makes it ")
                print("[Warning] impossible to use true seeking! Please (re)compress your TAR using multiple ")
                print("[Warning] frames in order for ratarmount to do be able to do fast seeking to requested ")
                print("[Warning] files. Else, each file access will decompress the whole TAR from the beginning!")
                print("[Warning] You can try out t2sz for creating such archives:")
                print("[Warning] https://github.com/martinellimarco/t2sz")
                print("[Warning] Here you can find a simple bash script demonstrating how to do this:")
                print("[Warning] https://github.com/mxmlnkn/ratarmount#xz-and-zst-files")
                print()
        except Exception:
            pass

        if compression not in supportedCompressions:
            if detectRawTar(fileobj, encoding):
                return path, compression

            if (
                compression is None
                and sqlcipher3 is not None
                and path.lower().endswith(".sqlar")
                and isRandom(fileobj.read(4096))
            ):
                return path, 'sqlar'

            if printDebug >= 2:
                print(f"Archive '{path}' (compression: {compression}) cannot be opened!")

            if printDebug >= 1:
                print("[Info] Supported compressions:", list(supportedCompressions.keys()))
                if 'deb' not in supportedCompressions:
                    print("[Warning] It seems that the libarchive backend is not available. Try installing it with:")
                    print("[Warning]  - apt install libarchive13")
                    print("[Warning]  - yum install libarchive")

            raise argparse.ArgumentTypeError(f"Archive '{path}' cannot be opened!")

    if not findAvailableBackend(compression):
        moduleNames = [module.name for module in supportedCompressions[compression].modules]
        raise argparse.ArgumentTypeError(
            f"Cannot open a {compression} compressed TAR file '{fileobj.name}' "
            f"without any of these modules: {moduleNames}"
        )

    return path, compression
