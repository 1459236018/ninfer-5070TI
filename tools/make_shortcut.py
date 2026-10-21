# -*- coding: utf-8 -*-
"""
make_shortcut.py -- create a Windows .lnk shortcut WITHOUT COM/WScript.Shell,
by writing the MS-SHLLINK binary format directly.

Usage:
  python make_shortcut.py --target <file.bat> --workdir <dir> [--icon <exe>]
                          [--name <shortcut name>] [--outdir <dir>]
                          [--minimized]
"""
import argparse
import ctypes
import os
import struct
import sys

CLSID_SHELLLINK = bytes([
    0x01, 0x14, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00,
    0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46,
])

# LinkFlags
HAS_LINK_TARGET_ID_LIST = 0x00000001
HAS_LINK_INFO = 0x00000002
HAS_NAME = 0x00000004
HAS_RELATIVE_PATH = 0x00000008
HAS_WORKING_DIR = 0x00000010
HAS_ARGUMENTS = 0x00000020
HAS_ICON_LOCATION = 0x00000040
IS_UNICODE = 0x00000080

DRIVE_FIXED = 3


def ansi(s):
    """ANSI (codepage) null-terminated string."""
    try:
        raw = s.encode('mbcs')
    except Exception:
        raw = s.encode('latin-1', 'replace')
    return raw + b'\x00'


def ustr(s):
    """Counted unicode string: u16 char count + utf-16le.

    NOTE: no trailing null. The count is authoritative -- verified empirically
    against 25 Windows-authored shortcuts on this machine (all parse under an
    implementation that does not consume a terminator).
    """
    return struct.pack('<H', len(s)) + s.encode('utf-16-le')


def volume_serial(root):
    serial = ctypes.c_ulong(0)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root), None, 0,
        ctypes.byref(serial), None, None, None, 0)
    return serial.value if ok else 0


def build_idlist(target):
    """Build a LinkTargetIDList identical in shape to the one Windows writes.

    Delegates the Shell Item encoding to pylnk3 (a battle-tested parser/writer)
    by writing a throwaway .lnk and extracting its IDList bytes.
    """
    import io
    import tempfile
    import pylnk3

    tmp = os.path.join(tempfile.mkdtemp(prefix='lnk_'), 'probe.lnk')
    pylnk3.for_file(target, lnk_name=tmp)
    with open(tmp, 'rb') as f:
        raw = f.read()
    os.remove(tmp)
    os.rmdir(os.path.dirname(tmp))

    assert struct.unpack_from('<I', raw, 0)[0] == 0x4C, 'bad probe header'
    flags = struct.unpack_from('<I', raw, 20)[0]
    assert flags & HAS_LINK_TARGET_ID_LIST, 'probe has no IDList'
    size = struct.unpack_from('<H', raw, 76)[0]      # IDListSize (incl. itself)
    return raw[76:76 + size + 2]                     # + 2-byte terminator


def build(target, workdir, icon, description, args='', show_cmd=1, idlist=None):
    target_ansi = ansi(target)
    file_size = os.path.getsize(target) if os.path.isfile(target) else 0

    # ---- LinkInfo ----
    label = ansi('')                                  # empty volume label
    volid = (struct.pack('<I', 16 + len(label))        # VolumeIDSize
             + struct.pack('<III', DRIVE_FIXED,
                           volume_serial(os.path.splitdrive(target)[0] + '\\'),
                           0x10)                      # DriveType, Serial, LabelOffset
             + label)
    suffix = b'\x00'                                   # CommonPathSuffix = ""
    vol_off = 0x1C
    lbp_off = vol_off + len(volid)
    cps_off = lbp_off + len(target_ansi)
    link_info_size = cps_off + len(suffix)
    link_info = (struct.pack('<IIIIIII', link_info_size, 0x1C, 0x1,
                             vol_off, lbp_off, 0, cps_off)
                 + volid + target_ansi + suffix)

    # ---- StringData (order fixed by spec: NAME, RELATIVE, WORKINGDIR, ARGS, ICON) ----
    flags = HAS_LINK_INFO | IS_UNICODE
    if idlist:
        flags |= HAS_LINK_TARGET_ID_LIST
    stringdata = b''
    if description:
        flags |= HAS_NAME
        stringdata += ustr(description)
    if workdir:
        flags |= HAS_WORKING_DIR
        stringdata += ustr(workdir)
    if args:
        flags |= HAS_ARGUMENTS
        stringdata += ustr(args)
    if icon:
        flags |= HAS_ICON_LOCATION
        stringdata += ustr(icon)

    # ---- ShellLinkHeader (76 bytes) ----
    header = struct.pack('<I', 0x4C) + CLSID_SHELLLINK
    header += struct.pack('<I', flags)
    header += struct.pack('<I', 0x20)          # FILE_ATTRIBUTE_ARCHIVE
    header += b'\x00' * 24                     # Creation/Access/Write time
    header += struct.pack('<I', file_size)
    header += struct.pack('<i', 0)             # IconIndex
    header += struct.pack('<I', show_cmd)      # ShowCommand
    header += struct.pack('<H', 0)             # HotKey
    header += b'\x00' * 10                     # Reserved (2 + 4 + 4)
    assert len(header) == 76, len(header)

    return header + (idlist or b'') + link_info + stringdata + struct.pack('<I', 0)


def parse(data):
    """Minimal round-trip sanity check."""
    assert struct.unpack_from('<I', data, 0)[0] == 0x4C
    assert data[4:20] == CLSID_SHELLLINK
    flags = struct.unpack_from('<I', data, 20)[0]
    pos = 76
    info = {}
    if flags & HAS_LINK_TARGET_ID_LIST:
        size = struct.unpack_from('<H', data, pos)[0]
        info['idlist_size'] = size
        pos += size + 2
    if flags & HAS_LINK_INFO:
        size, hdr_size, li_flags, v_off, lbp_off, cnrl_off, cps_off = \
            struct.unpack_from('<IIIIIII', data, pos)
        base = pos
        pl = data[base + lbp_off: data.index(b'\x00', base + lbp_off)]
        info['local_base_path'] = pl.decode('mbcs')
        info['link_info_size'] = size
        pos += size
    if flags & IS_UNICODE:
        if flags & HAS_NAME:
            n = struct.unpack_from('<H', data, pos)[0]
            info['name'] = data[pos + 2:pos + 2 + n * 2].decode('utf-16-le')
            pos += 2 + n * 2
        if flags & HAS_RELATIVE_PATH:
            n = struct.unpack_from('<H', data, pos)[0]
            info['relative'] = data[pos + 2:pos + 2 + n * 2].decode('utf-16-le')
            pos += 2 + n * 2
        if flags & HAS_WORKING_DIR:
            n = struct.unpack_from('<H', data, pos)[0]
            info['workdir'] = data[pos + 2:pos + 2 + n * 2].decode('utf-16-le')
            pos += 2 + n * 2
        if flags & HAS_ARGUMENTS:
            n = struct.unpack_from('<H', data, pos)[0]
            info['args'] = data[pos + 2:pos + 2 + n * 2].decode('utf-16-le')
            pos += 2 + n * 2
        if flags & HAS_ICON_LOCATION:
            n = struct.unpack_from('<H', data, pos)[0]
            info['icon'] = data[pos + 2:pos + 2 + n * 2].decode('utf-16-le')
            pos += 2 + n * 2
    info['flags'] = hex(flags)
    info['trailing'] = len(data) - pos
    return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', required=True)
    ap.add_argument('--workdir', default='')
    ap.add_argument('--icon', default='')
    ap.add_argument('--name', required=True)
    ap.add_argument('--outdir', default='')
    ap.add_argument('--minimized', action='store_true')
    ap.add_argument('--args', default='',
                    help='command-line arguments to store in the shortcut')
    ap.add_argument('--no-idlist', action='store_true',
                    help='omit the target ID list (not recommended: Explorer '
                         'needs it)')
    a = ap.parse_args()

    if os.name == 'nt':
        # POSIX-style shells (Git Bash / MSYS) mangle backslashes into '/',
        # which Windows path APIs accept but the shell-link format should not carry.
        a.target = os.path.normpath(a.target)
        a.workdir = os.path.normpath(a.workdir) if a.workdir else ''
        a.icon = os.path.normpath(a.icon) if a.icon else ''

    outdir = a.outdir or os.path.join(os.path.expanduser('~'), 'Desktop')
    out = os.path.join(outdir, a.name if a.name.lower().endswith('.lnk')
                       else a.name + '.lnk')

    idlist = None
    if not a.no_idlist:
        try:
            idlist = build_idlist(a.target)
            print('idlist  : %d bytes (from pylnk3)' % len(idlist))
        except ImportError:
            print('idlist  : SKIPPED -- pylnk3 not installed in this interpreter')
            print('          (run with the venv python that has pylnk3)')

    data = build(a.target, a.workdir, a.icon, a.name, a.args,
                 show_cmd=7 if a.minimized else 1, idlist=idlist)
    with open(out, 'wb') as f:
        f.write(data)

    linked = 'N/A'
    if os.path.lexists(out):
        try:
            linked = str(os.path.getsize(out))
        except Exception:
            pass
    print('written : %s' % out.encode('utf-8', 'replace').decode('utf-8'))
    print('bytes   : %d' % len(data))
    print('verify  : %r' % (parse(data),))


if __name__ == '__main__':
    main()
