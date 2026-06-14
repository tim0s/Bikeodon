"""Synthesise a FIT file from Strava stream data."""

import struct
from datetime import datetime, timezone

_FIT_EPOCH = 631065600  # Unix timestamp of 1989-12-31 00:00:00 UTC

_CRC_TABLE = [
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
]


def _crc(data: bytes, crc: int = 0) -> int:
    for byte in data:
        crc_new = (byte ^ crc) & 0x0F
        crc >>= 4
        crc ^= _CRC_TABLE[crc_new]
        crc_new = (byte >> 4) & 0x0F
        crc ^= _CRC_TABLE[crc_new]
    return crc


def _unix_to_fit(unix_ts: float) -> int:
    return max(0, int(unix_ts) - _FIT_EPOCH)


def _deg_to_semi(deg: float) -> int:
    return int(round(deg * (2 ** 31 / 180.0)))


# (base_type_byte, struct_format, size_bytes, invalid_value)
_ENUM   = (0x00, "B",  1, 0xFF)
_UINT8  = (0x02, "B",  1, 0xFF)
_SINT8  = (0x01, "b",  1, 0x7F)
_SINT16 = (0x83, "<h", 2, 0x7FFF)
_UINT16 = (0x84, "<H", 2, 0xFFFF)
_SINT32 = (0x85, "<i", 4, 0x7FFFFFFF)
_UINT32 = (0x86, "<I", 4, 0xFFFFFFFF)


def _def_msg(local_num: int, global_num: int, fields: list) -> bytes:
    out = bytearray([
        0x40 | (local_num & 0x0F),  # definition message header
        0x00,                        # reserved
        0x00,                        # architecture: little-endian
    ])
    out += struct.pack("<H", global_num)
    out += bytes([len(fields)])
    for field_num, (bt_byte, _, size, _invalid) in fields:
        out += bytes([field_num, size, bt_byte])
    return bytes(out)


def _data_msg(local_num: int, fields: list, values: list) -> bytes:
    out = bytearray([local_num & 0x0F])  # data message header
    for (_, (_, fmt, _, invalid)), val in zip(fields, values):
        v = invalid if val is None else val
        if fmt in ("<H", "<h", "<I", "<i"):
            out += struct.pack(fmt, v)
        elif fmt == "b":
            out += struct.pack("b", v)
        else:
            out += bytes([v & 0xFF])
    return bytes(out)


_SPORT_MAP = {
    "Ride": 2, "VirtualRide": 2, "EBikeRide": 2, "Handcycle": 2,
    "Run": 1, "VirtualRun": 1, "TrailRun": 1,
    "Walk": 11, "Hike": 17,
    "Swim": 5, "OpenWaterSwim": 5,
    "Workout": 4, "WeightTraining": 4, "Yoga": 4,
}


def generate_fit(activity: dict, streams: dict) -> bytes:
    """Generate a binary FIT file from a Bikeodon activity dict and raw Strava streams.

    activity: as returned by StravaClient.get_activity()[0]
    streams:  raw stream dict from StravaClient, keyed by stream type
    """
    time_s   = streams.get("time",            {}).get("data", [])
    latlng   = streams.get("latlng",          {}).get("data", [])
    altitude = streams.get("altitude",        {}).get("data", [])
    hr       = streams.get("heartrate",       {}).get("data", [])
    cadence  = streams.get("cadence",         {}).get("data", [])
    watts    = streams.get("watts",           {}).get("data", [])
    distance = streams.get("distance",        {}).get("data", [])
    velocity = streams.get("velocity_smooth", {}).get("data", [])
    temp     = streams.get("temp",            {}).get("data", [])
    grade    = streams.get("grade_smooth",    {}).get("data", [])

    start_dt   = datetime.fromisoformat(
        activity.get("start_date", "1970-01-01T00:00:00Z").replace("Z", "+00:00")
    )
    start_fit = _unix_to_fit(start_dt.timestamp())
    sport     = _SPORT_MAP.get(activity.get("sport_type", ""), 0)
    n         = len(time_s)

    # Build record field list from whichever streams are present
    record_fields = [(253, _UINT32)]  # timestamp — always included
    if latlng:
        record_fields += [(0, _SINT32), (1, _SINT32)]
    if altitude:
        record_fields.append((2, _UINT16))
    if hr:
        record_fields.append((3, _UINT8))
    if cadence:
        record_fields.append((4, _UINT8))
    if distance:
        record_fields.append((5, _UINT32))
    if velocity:
        record_fields.append((6, _UINT16))
    if watts:
        record_fields.append((7, _UINT16))
    if temp:
        record_fields.append((13, _SINT8))
    if grade:
        record_fields.append((9, _SINT16))

    def _at(lst, i):
        return lst[i] if i < len(lst) else None

    body = bytearray()

    # file_id (local 0, global 0): type=activity, manufacturer=development
    fid_fields = [(0, _ENUM), (1, _UINT16), (4, _UINT32)]
    body += _def_msg(0, 0, fid_fields)
    body += _data_msg(0, fid_fields, [4, 255, start_fit])

    # record (local 1, global 20)
    if n > 0:
        body += _def_msg(1, 20, record_fields)
    for i in range(n):
        t = _at(time_s, i)
        ts = start_fit + (int(t) if t is not None else i)
        vals = [ts]
        if latlng:
            ll = _at(latlng, i)
            vals += [_deg_to_semi(ll[0]), _deg_to_semi(ll[1])] if ll else [None, None]
        if altitude:
            a = _at(altitude, i)
            vals.append(max(0, int((a + 500) * 5)) if a is not None else None)
        if hr:
            h = _at(hr, i)
            vals.append(int(h) if h is not None else None)
        if cadence:
            c = _at(cadence, i)
            vals.append(int(c) if c is not None else None)
        if distance:
            d = _at(distance, i)
            vals.append(int(d * 100) if d is not None else None)
        if velocity:
            v = _at(velocity, i)
            vals.append(int(v * 1000) if v is not None else None)
        if watts:
            w = _at(watts, i)
            vals.append(int(w) if w is not None else None)
        if temp:
            t2 = _at(temp, i)
            vals.append(int(t2) if t2 is not None else None)
        if grade:
            g = _at(grade, i)
            vals.append(int(g * 100) if g is not None else None)
        body += _data_msg(1, record_fields, vals)

    # session (local 2, global 18)
    end_fit    = start_fit + (int(time_s[-1]) if time_s else 0)
    elapsed_ms = int((activity.get("elapsed_time") or 0) * 1000)
    timer_ms   = int((activity.get("moving_time")  or 0) * 1000)
    dist_cm    = int((activity.get("distance")      or 0) * 100)
    d_m = activity.get("distance") or 0
    t_s = activity.get("moving_time") or 0
    avg_spd_mms = int(d_m / t_s * 1000) if t_s else None
    max_spd     = activity.get("max_speed")
    max_spd_mms = int(max_spd * 1000) if max_spd else None
    avg_pwr = int(activity.get("average_watts")     or 0) or None
    max_pwr = int(activity.get("max_watts")         or 0) or None
    avg_hr  = int(activity.get("average_heartrate") or 0) or None
    max_hr  = int(activity.get("max_heartrate")     or 0) or None
    asc     = int(activity.get("total_elevation_gain") or 0) or None

    ses_fields = [
        (253, _UINT32), (0, _ENUM), (1, _ENUM), (2, _UINT32), (5, _ENUM),
        (7, _UINT32), (8, _UINT32), (9, _UINT32),
        (20, _UINT16), (21, _UINT16), (22, _UINT16), (23, _UINT16),
        (26, _UINT16), (44, _UINT8), (45, _UINT8),
    ]
    ses_vals = [
        end_fit, 8, 1, start_fit, sport,
        elapsed_ms, timer_ms, dist_cm,
        avg_spd_mms, max_spd_mms, avg_pwr, max_pwr,
        asc, avg_hr, max_hr,
    ]
    body += _def_msg(2, 18, ses_fields)
    body += _data_msg(2, ses_fields, ses_vals)

    # activity (local 3, global 34)
    act_fields = [(253, _UINT32), (1, _UINT16), (2, _ENUM), (3, _ENUM), (4, _ENUM)]
    body += _def_msg(3, 34, act_fields)
    body += _data_msg(3, act_fields, [end_fit, 1, 0, 26, 1])

    body = bytes(body)

    hdr = struct.pack("<BBHI4s", 14, 0x10, 2100, len(body), b".FIT")
    hdr += struct.pack("<H", _crc(hdr))
    out = hdr + body
    return out + struct.pack("<H", _crc(out))
