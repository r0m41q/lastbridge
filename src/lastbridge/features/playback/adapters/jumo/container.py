from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

INIT_UUID = bytes.fromhex("c7c75df0fdd951e98fc22971e4acf8d2")
SEGMENT_UUID = bytes.fromhex("3b42129256f35f75923663b69a1f52b2")


@dataclass(frozen=True)
class TableEntry:
    byte_length: int
    sample_count: int


@dataclass(frozen=True)
class InitInfo:
    kind: str
    header: bytes
    table: tuple[TableEntry, ...]


@dataclass(frozen=True)
class Frame:
    size: int
    flags: int
    iv: bytes


@dataclass(frozen=True)
class SegmentLayout:
    data_start: int
    mdat_end: int
    frames: tuple[Frame, ...]


def boxes(data: bytes):
    position = 0
    while position + 8 <= len(data):
        size = int.from_bytes(data[position: position + 4], "big")
        if size == 1:
            size = len(data) - position
        if size < 8 or position + size > len(data):
            break
        yield data[position + 4: position + 8], position, position + size
        position += size


def uuid_box(data: bytes, user_type: bytes):
    for kind, start, end in boxes(data):
        if kind == b"uuid" and start + 24 <= end:
            if data[start + 8: start + 24] == user_type:
                return start, end
    return None


def parse_init(data: bytes) -> InitInfo:
    box = uuid_box(data, INIT_UUID)
    if box is None:
        raise ValueError("Jumo init segment has no init box")
    payload = data[box[0] + 24: box[1]]
    if len(payload) < 28:
        raise ValueError("Jumo init payload is too short")
    key_length = int.from_bytes(payload[26:28], "big")
    region = payload[: min(len(payload), 28 + key_length)]
    position = 28 + key_length
    table: list[TableEntry] = []
    if position < len(payload):
        position += 1 + payload[position]
        if position + 2 <= len(payload):
            count = int.from_bytes(payload[position: position + 2], "big")
            position += 2
            for _ in range(count):
                if position + 8 > len(payload):
                    break
                table.append(
                    TableEntry(
                        int.from_bytes(
                            payload[position: position + 4], "big"
                        ),
                        int.from_bytes(
                            payload[position + 4: position + 8], "big"
                        ),
                    )
                )
                position += 8
    flac = region.find(b"fLaC")
    if flac >= 0 and flac + 42 <= len(region):
        header = bytearray(region[flac: flac + 42])
        header[4] |= 0x80
        return InitInfo("flac", bytes(header), tuple(table))
    return InitInfo("mp4", data, tuple(table))


def parse_segment(data: bytes) -> SegmentLayout:
    box = uuid_box(data, SEGMENT_UUID)
    if box is None:
        raise ValueError("Jumo segment has no segment box")
    mdat_end = next(
        (end for kind, _, end in boxes(data) if kind == b"mdat"), len(data)
    )
    payload = data[box[0] + 24:]
    if len(payload) < 12:
        raise ValueError("Jumo segment index is too short")
    data_offset = int.from_bytes(payload[4:8], "big")
    entry_size = payload[8]
    count = int.from_bytes(payload[9:12], "big")
    stride = 8 + entry_size
    frames = []
    position = 12
    for _ in range(count):
        if position + stride > len(payload):
            raise ValueError("Jumo segment index is truncated")
        frames.append(
            Frame(
                int.from_bytes(payload[position: position + 4], "big"),
                int.from_bytes(payload[position + 6: position + 8], "big"),
                payload[position + 8: position + 8 + min(entry_size, 16)],
            )
        )
        position += stride
    return SegmentLayout(box[0] + data_offset, mdat_end, tuple(frames))


def decrypt_frame(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    counter = bytearray(16)
    counter[: min(8, len(iv))] = iv[:8]
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    output = bytearray()
    for offset in range(0, len(ciphertext), 16):
        stream = encryptor.update(bytes(counter))
        chunk = ciphertext[offset: offset + 16]
        output.extend(a ^ b for a, b in zip(chunk, stream, strict=False))
        for index in range(15, 7, -1):
            counter[index] = (counter[index] + 1) & 255
            if counter[index]:
                break
    return bytes(output)


def write_segment(
    init: InitInfo, index: int, data: bytes, key: bytes, output
) -> None:
    layout = parse_segment(data)
    mdat_bytes = layout.mdat_end - layout.data_start
    if index >= len(init.table) or mdat_bytes != init.table[index].byte_length:
        raise ValueError("Jumo segment is truncated")
    position = layout.data_start
    for frame in layout.frames:
        if position + frame.size > len(data):
            raise ValueError("Jumo frame overruns segment")
        chunk = data[position: position + frame.size]
        output.write(
            chunk if frame.flags == 0 else decrypt_frame(key, frame.iv, chunk)
        )
        position += frame.size
    if position < layout.mdat_end:
        output.write(data[position: layout.mdat_end])
