"""Read public packet headers only. Never decrypt or retain media payloads."""

import re


def answer_metadata(body, previous=None):
    text = body.decode("utf-8")
    payloads = dict(previous or {})
    kind = None
    for line in text.splitlines():
        if line.startswith("m="):
            kind = line[2:].split()[0]
        match = re.match(r"a=rtpmap:(\d+) ", line)
        if match and kind in ("audio", "video"):
            payloads[int(match[1])] = kind
    ufrags = set(re.findall(r"^a=ice-ufrag:([^\r\n]+)", text, re.MULTILINE))
    return ufrags, payloads


def stun_usernames(data):
    if len(data) < 20 or data[0] > 3 or data[4:8] != b"\x21\x12\xa4\x42":
        return ()
    end = min(len(data), 20 + int.from_bytes(data[2:4], "big"))
    offset = 20
    while offset + 4 <= end:
        tag = int.from_bytes(data[offset : offset + 2], "big")
        size = int.from_bytes(data[offset + 2 : offset + 4], "big")
        if offset + 4 + size > end:
            break
        if tag == 6:  # STUN USERNAME: remote/local ICE fragments, not persisted.
            return tuple(data[offset + 4 : offset + 4 + size].decode(errors="replace").split(":"))
        offset += 4 + ((size + 3) // 4) * 4
    return ()


def packet_kind(data, payloads):
    # RFC 7983 demultiplexing; RFC 5761 reserves RTCP's second-byte range.
    if not data:
        return "other"
    if data[0] <= 3:
        return "stun"
    if 20 <= data[0] <= 63:
        return "dtls"
    if 128 <= data[0] <= 191 and len(data) >= 2:
        if 192 <= data[1] <= 223:
            return "rtcp"
        if len(data) >= 12:
            return payloads.get(data[1] & 127, "rtp")
    return "other"
