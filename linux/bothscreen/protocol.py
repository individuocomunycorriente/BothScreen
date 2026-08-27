"""Protocolo de cable entre el daemon de Linux y la app Android.

Todo el tráfico va sobre un único socket TCP que llega a la tablet a través de
`adb reverse`, así que el enlace físico es el cable USB-C.

Todos los enteros son big-endian (orden de red) para que el lado Kotlin pueda
usar DataInputStream/DataOutputStream directamente sin invertir bytes.

    HELLO   (tablet -> PC)
        magic          4 bytes  b"BSCR"
        version        u32
        width          u32     ancho físico del panel de la tablet
        height         u32     alto físico del panel de la tablet
        max_fps        u32
        codecs         u32     máscara de bits: 1=H.264, 2=HEVC
        name_len       u32
        name           name_len bytes UTF-8

    CONFIG  (PC -> tablet)
        magic          4 bytes  b"BSCR"
        version        u32
        codec          u32     0=H.264, 1=HEVC
        width          u32     resolución que se va a transmitir
        height         u32
        fps            u32
        bitrate_kbps   u32

    FRAME   (PC -> tablet)      se repite indefinidamente
        type           u8      0
        flags          u8      bit0=CSD/config, bit1=keyframe
        pts_us         u64
        size           u32
        payload        size bytes (unidad de acceso Annex-B)

    STATS   (PC -> tablet)      informativo, la app lo muestra en el HUD
        type           u8      1
        bitrate_kbps   u32
        fps_x10        u32

    ACK     (tablet -> PC)
        type           u8      1
        pts_us         u64

    KEYFRAME_REQUEST (tablet -> PC)
        type           u8      2
"""

import struct

MAGIC = b"BSCR"
PROTO_VERSION = 1

CODEC_H264 = 0
CODEC_HEVC = 1

CODEC_BIT_H264 = 1
CODEC_BIT_HEVC = 2

MSG_FRAME = 0
MSG_STATS = 1

CTL_ACK = 1
CTL_REQUEST_KEYFRAME = 2

FLAG_CONFIG = 1
FLAG_KEYFRAME = 2

HELLO_FIXED = struct.Struct(">4sIIIIII")
CONFIG_STRUCT = struct.Struct(">4sIIIIII")
FRAME_HEADER = struct.Struct(">BBQI")
STATS_STRUCT = struct.Struct(">BII")
ACK_STRUCT = struct.Struct(">Q")

MIME_BY_CODEC = {CODEC_H264: "video/avc", CODEC_HEVC: "video/hevc"}


def pack_config(codec, width, height, fps, bitrate_kbps):
    return CONFIG_STRUCT.pack(
        MAGIC, PROTO_VERSION, codec, width, height, fps, bitrate_kbps
    )


def pack_frame(flags, pts_us, payload):
    return FRAME_HEADER.pack(MSG_FRAME, flags, pts_us, len(payload)) + payload


def pack_stats(bitrate_kbps, fps):
    return STATS_STRUCT.pack(MSG_STATS, int(bitrate_kbps), int(fps * 10))


def parse_hello(buf):
    magic, version, width, height, max_fps, codecs, name_len = HELLO_FIXED.unpack(
        buf[: HELLO_FIXED.size]
    )
    if magic != MAGIC:
        raise ValueError("magic inválido en HELLO: %r" % (magic,))
    return {
        "version": version,
        "width": width,
        "height": height,
        "max_fps": max_fps,
        "codecs": codecs,
        "name_len": name_len,
    }
