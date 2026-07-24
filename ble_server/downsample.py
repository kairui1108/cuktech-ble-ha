"""LTTB (Largest Triangle Three Buckets) downsampling for power curves."""


def lttb_downsample(points: list[dict], target: int) -> list[dict]:
    """Downsample power curve data using LTTB algorithm."""
    n = len(points)
    if n <= target:
        return points
    if target < 3:
        return [points[0], points[-1]]

    # Pre-extract columns to eliminate dict key lookups in hot loops
    ts = [p["timestamp"] for p in points]
    power = [p["power"] for p in points]

    result = [points[0]]
    bucket_size = (n - 2) / (target - 2)

    for i in range(1, target - 1):
        # Cache bucket boundaries (calculated once)
        bucket_start = int((i - 1) * bucket_size) + 1
        bucket_end = min(int(i * bucket_size), n)
        avg_start = int(i * bucket_size)
        avg_end = min(int((i + 1) * bucket_size), n)
        avg_len = avg_end - avg_start

        # Average of next bucket — uses pre-extracted lists, no dict lookups
        avg_ts_v = sum(ts[avg_start:avg_end]) / avg_len
        avg_power_v = sum(power[avg_start:avg_end]) / avg_len

        # Find point in current bucket with max triangle area
        max_area = -1.0
        max_idx = bucket_start

        prev = result[-1]
        prev_ts = prev["timestamp"]
        prev_power = prev["power"]

        for j in range(bucket_start, bucket_end):
            # Triangle area without / 2.0 (comparison unaffected)
            area = (prev_ts - avg_ts_v) * (power[j] - prev_power) - \
                   (prev_ts - ts[j]) * (avg_power_v - prev_power)
            if area < 0:
                area = -area
            if area > max_area:
                max_area = area
                max_idx = j

        result.append(points[max_idx])

    result.append(points[-1])
    return result
