"""Tests for downsample.py - LTTB downsampling algorithm."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from downsample import lttb_downsample


def _make_point(ts, power, **extra):
    p = {"timestamp": ts, "power": power}
    p.update(extra)
    return p


class TestLTTBDownsample:
    """Test LTTB downsampling algorithm."""

    def test_input_equals_target_returns_same(self):
        """When len(points) == target, return points unchanged."""
        points = [_make_point(i, float(i * 10)) for i in range(5)]
        result = lttb_downsample(points, 5)
        assert result is points

    def test_input_less_than_target_returns_same(self):
        """When len(points) < target, return points unchanged."""
        points = [_make_point(i, float(i * 10)) for i in range(3)]
        result = lttb_downsample(points, 10)
        assert result is points

    def test_first_and_last_preserved(self):
        """First and last points must be in output."""
        points = [_make_point(i, float(i)) for i in range(100)]
        result = lttb_downsample(points, 10)
        assert result[0] == points[0]
        assert result[-1] == points[-1]

    def test_target_count_correct(self):
        """Output has exactly 'target' points."""
        points = [_make_point(i, float(i)) for i in range(100)]
        result = lttb_downsample(points, 20)
        assert len(result) == 20

    def test_downsample_preserves_peak(self):
        """A sharp peak should be preserved in downsampled output."""
        points = [_make_point(i, 1.0) for i in range(100)]
        points[50] = _make_point(50, 100.0)  # peak
        result = lttb_downsample(points, 10)
        # At least one point should have power > 50 (peak preserved)
        assert any(p["power"] > 50 for p in result)

    def test_downsample_two_points(self):
        """Minimum case: 2 input points."""
        points = [_make_point(0, 10.0), _make_point(10, 20.0)]
        result = lttb_downsample(points, 2)
        assert len(result) == 2
        assert result == points

    def test_downsample_three_to_two(self):
        """3 points downsampled to 2: only first and last."""
        points = [_make_point(0, 10.0), _make_point(5, 100.0), _make_point(10, 20.0)]
        result = lttb_downsample(points, 2)
        assert len(result) == 2
        assert result[0] == points[0]
        assert result[-1] == points[-1]

    def test_uniform_data(self):
        """All points same value → downsampled points also same."""
        points = [_make_point(i, 50.0) for i in range(50)]
        result = lttb_downsample(points, 10)
        assert len(result) == 10
        for p in result:
            assert p["power"] == 50.0

    def test_extra_fields_preserved(self):
        """Extra fields like voltage/current should be preserved."""
        points = [
            _make_point(i, float(i * 2), voltage=20.0 + i * 0.1, current=1.0)
            for i in range(50)
        ]
        result = lttb_downsample(points, 10)
        assert len(result) == 10
        for p in result:
            assert "voltage" in p
            assert "current" in p

    def test_monotonic_timestamps(self):
        """Output timestamps must be in order (algorithm preserves order)."""
        points = [_make_point(i, float(i % 10 * 10)) for i in range(200)]
        result = lttb_downsample(points, 30)
        timestamps = [p["timestamp"] for p in result]
        assert timestamps == sorted(timestamps)

    def test_large_input(self):
        """Handle 10000 points without error."""
        points = [_make_point(i, float(i % 100)) for i in range(10000)]
        result = lttb_downsample(points, 500)
        assert len(result) == 500
