"""Bounded network deadlines; never delays or batches an input event."""
import math

from .config import CHALLENGE_INTERVAL, MAX_QUEUE_AGE, NETWORK_TIMEOUT_MAX, NETWORK_TIMEOUT_MIN


class NetworkTiming:
    def __init__(self):
        self.rtt = None
        self.variation = 0.0

    def observe(self, seconds):
        if not math.isfinite(seconds) or not 0 <= seconds < NETWORK_TIMEOUT_MAX:
            return False
        if self.rtt is None:
            self.rtt, self.variation = seconds, seconds / 2
        else:
            # Smooth variation against the previous RTT, then update the RTT.
            self.variation = .75 * self.variation + .25 * abs(self.rtt - seconds)
            self.rtt = .875 * self.rtt + .125 * seconds
        return True

    @property
    def timeout(self):
        if self.rtt is None:
            return NETWORK_TIMEOUT_MIN
        estimate = self.rtt + 4 * self.variation + CHALLENGE_INTERVAL
        return min(NETWORK_TIMEOUT_MAX, max(NETWORK_TIMEOUT_MIN, estimate))

    @property
    def input_timeout(self):
        # Input can use the previous challenge and one permitted local queue
        # interval. A token's age includes the whole challenge/echo round trip.
        return self.timeout + CHALLENGE_INTERVAL + MAX_QUEUE_AGE
