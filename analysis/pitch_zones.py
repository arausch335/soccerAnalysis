"""Shared Opta-style pitch-zone geometry for provider coordinates on a 0-100 scale."""

PITCH_WIDTH_M = 68.0
GOAL_WIDTH_M = 7.32
GOAL_AREA_SIDE_M = (PITCH_WIDTH_M - (GOAL_WIDTH_M + 2 * 5.5)) / 2
PENALTY_AREA_SIDE_M = (PITCH_WIDTH_M - (GOAL_WIDTH_M + 2 * 16.5)) / 2

# The center channel spans the six-yard-box width. Half-spaces extend from the
# six-yard-box sides to the penalty-area sides; wings occupy the remainder.
RIGHT_WING_MAX = PENALTY_AREA_SIDE_M / PITCH_WIDTH_M * 100
RIGHT_HALF_SPACE_MAX = GOAL_AREA_SIDE_M / PITCH_WIDTH_M * 100
LEFT_HALF_SPACE_MIN = 100 - RIGHT_HALF_SPACE_MAX
LEFT_WING_MIN = 100 - RIGHT_WING_MAX

CHANNELS = ('right_wing', 'right_half_space', 'central', 'left_half_space', 'left_wing')
LONGITUDINAL_BINS = 6
LATERAL_BINS = len(CHANNELS)


def channel(y, *, center='central'):
    """Return the box-aligned lateral channel for a valid 0-100 coordinate."""
    if y < RIGHT_WING_MAX:
        return 'right_wing'
    if y < RIGHT_HALF_SPACE_MAX:
        return 'right_half_space'
    if y < LEFT_HALF_SPACE_MIN:
        return center
    if y < LEFT_WING_MIN:
        return 'left_half_space'
    return 'left_wing'


def lateral_index(y):
    return CHANNELS.index(channel(y))


def longitudinal_index(x):
    return min(int(x / (100 / LONGITUDINAL_BINS)), LONGITUDINAL_BINS - 1)


def zone_number(x, y):
    """Number 30 zones from own goal to opponent goal, bottom to top per slice.

    Zones 1-5 are the deepest sixth and zones 26-30 the most advanced sixth.
    Within each slice the order is right wing, right half-space, center, left
    half-space, left wing, matching provider y from 0 through 100.
    """
    return longitudinal_index(x) * LATERAL_BINS + lateral_index(y) + 1


def coarse_zone_index(x, y):
    """Return the zero-based three-thirds-by-five-channels region index."""
    third = min(int(x / (100 / 3)), 2)
    return third * LATERAL_BINS + lateral_index(y)
