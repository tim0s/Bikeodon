"""
Minimal FIT (Flexible and Interoperable Data Transfer) binary writer.

Only implements what's needed to emit a structured workout: FileId, Workout,
and WorkoutStep global messages with power targets. Not a general-purpose
FIT encoder.

Reference: Garmin FIT SDK / Profile.xlsx (global message numbers, field
numbers, and the CRC-16 algorithm below are stable across FIT SDK versions
and reproduced identically by every FIT reader/writer).
"""
import struct
import time

FIT_EPOCH_OFFSET = 631065600  # seconds between Unix epoch and FIT epoch (1989-12-31T00:00:00Z)

# base_type byte -> (byte value, size in bytes; None = variable/string)
ENUM, UINT8, STRING, UINT16, UINT32 = "enum", "uint8", "string", "uint16", "uint32"
_BASE_TYPE_BYTE = {ENUM: 0x00, UINT8: 0x02, STRING: 0x07, UINT16: 0x84, UINT32: 0x86}
_BASE_TYPE_STRUCT = {ENUM: "B", UINT8: "B", UINT16: "<H", UINT32: "<I"}

_CRC_TABLE = [
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
]


def _fit_crc16(data: bytes, crc: int = 0) -> int:
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


def _pack_value(value, base_type: str, size: int) -> bytes:
    if base_type == STRING:
        raw = (value or "").encode("utf-8")[:size - 1] + b"\x00"
        return raw.ljust(size, b"\x00")
    return struct.pack(_BASE_TYPE_STRUCT[base_type], int(value))


def _definition_message(local_type: int, global_mesg_num: int, fields: list) -> bytes:
    """fields: list of (field_def_num, size, base_type)."""
    header = bytes([0x40 | local_type])
    body = struct.pack("<BBH", 0, 0, global_mesg_num)  # reserved, architecture(LE), global mesg num
    body += bytes([len(fields)])
    for field_def_num, size, base_type in fields:
        body += bytes([field_def_num, size, _BASE_TYPE_BYTE[base_type]])
    return header + body


def _data_message(local_type: int, fields: list, values: list) -> bytes:
    header = bytes([local_type & 0x0F])
    body = b"".join(
        _pack_value(value, base_type, size)
        for (_, size, base_type), value in zip(fields, values)
    )
    return header + body


# global message numbers
_MESG_FILE_ID, _MESG_WORKOUT, _MESG_WORKOUT_STEP = 0, 26, 27

_FILE_ID_FIELDS = [(0, 1, ENUM), (1, 2, UINT16), (2, 2, UINT16), (4, 4, UINT32)]
_WORKOUT_FIELDS = [(4, 1, ENUM), (6, 2, UINT16), (8, 20, STRING)]
_WORKOUT_STEP_FIELDS = [
    (254, 2, UINT16),  # message_index
    (0, 24, STRING),   # wkt_step_name
    (1, 1, ENUM),      # duration_type
    (2, 4, UINT32),    # duration_value (ms)
    (3, 1, ENUM),      # target_type
    (5, 4, UINT32),    # custom_target_value_low
    (6, 4, UINT32),    # custom_target_value_high
    (7, 1, ENUM),      # intensity
]

_TARGET_TYPE_POWER = 4
_POWER_WATTS_OFFSET = 1000  # workout_power encoding: 0-1000 = %FTP, >1000 = watts + 1000

_INTENSITY_ACTIVE, _INTENSITY_REST, _INTENSITY_WARMUP, _INTENSITY_COOLDOWN = 0, 1, 2, 3


def _step_intensity(label: str) -> int:
    if label.startswith("Warmup"):
        return _INTENSITY_WARMUP
    if label.startswith("Cooldown"):
        return _INTENSITY_COOLDOWN
    if label.startswith("Recovery"):
        return _INTENSITY_REST
    return _INTENSITY_ACTIVE


def build_fit_workout(workout: dict) -> bytes:
    """workout: {"goal_label": str, "steps": [{"label", "duration_s", "watts"}, ...]}."""
    steps = workout["steps"]
    name = workout.get("goal_label", "Workout")

    records = b""
    records += _definition_message(0, _MESG_FILE_ID, _FILE_ID_FIELDS)
    records += _data_message(0, _FILE_ID_FIELDS, [
        5,                                                   # type = workout
        255,                                                 # manufacturer = development
        0,                                                   # product
        int(time.time()) - FIT_EPOCH_OFFSET,                 # time_created
    ])

    records += _definition_message(1, _MESG_WORKOUT, _WORKOUT_FIELDS)
    records += _data_message(1, _WORKOUT_FIELDS, [2, len(steps), name])  # sport=cycling

    records += _definition_message(2, _MESG_WORKOUT_STEP, _WORKOUT_STEP_FIELDS)
    for i, step in enumerate(steps):
        target = int(step["watts"]) + _POWER_WATTS_OFFSET
        records += _data_message(2, _WORKOUT_STEP_FIELDS, [
            i, step["label"], 0, int(round(step["duration_s"] * 1000)),
            _TARGET_TYPE_POWER, target, target, _step_intensity(step["label"]),
        ])

    header = struct.pack("<BBHI4s", 12, 0x10, 2132, len(records), b".FIT")
    crc = _fit_crc16(header + records)
    return header + records + struct.pack("<H", crc)
