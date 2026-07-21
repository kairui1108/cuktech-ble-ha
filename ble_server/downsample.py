"""LTTB (Largest Triangle Three Buckets) downsampling for power curves."""


def lttb_downsample(points: list[dict], target: int) -> list[dict]:
    """Downsample power curve data using LTTB algorithm.
    
    Args:
        points: list of dicts with at least 'timestamp' and 'power' keys
        target: target number of output points
    
    Returns:
        Downsampled list preserving visual shape
    """
    if len(points) <= target:
        return points

    # target >= 3 is required for the bucket algo; target=2 → just first+last
    if target < 3:
        return [points[0], points[-1]]

    result = [points[0]]
    bucket_size = (len(points) - 2) / (target - 2)
    
    for i in range(1, target - 1):
        # Average point of next bucket
        avg_start = int(i * bucket_size)
        avg_end = int((i + 1) * bucket_size)
        avg_end = min(avg_end, len(points))
        
        avg_ts = sum(p['timestamp'] for p in points[avg_start:avg_end]) / (avg_end - avg_start)
        avg_power = sum(p['power'] for p in points[avg_start:avg_end]) / (avg_end - avg_start)
        
        # Find point in current bucket with max triangle area
        bucket_start = int((i - 1) * bucket_size) + 1
        bucket_end = int(i * bucket_size)
        bucket_end = min(bucket_end, len(points))
        
        max_area = -1
        max_idx = bucket_start
        
        prev = result[-1]
        for j in range(bucket_start, bucket_end):
            area = abs(
                (prev['timestamp'] - avg_ts) * (points[j]['power'] - prev['power']) -
                (prev['timestamp'] - points[j]['timestamp']) * (avg_power - prev['power'])
            ) / 2.0
            if area > max_area:
                max_area = area
                max_idx = j
        
        result.append(points[max_idx])
    
    result.append(points[-1])
    return result
