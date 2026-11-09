"""One monotonic deadline for a whole setup phase, shared by the setup steps and the agent transport."""

import time


class Deadline:
    """Every wait and every RPC under it is capped by what is left; a step that finds it spent stops sending and
    says which phase ran out."""

    def __init__(self, budget=90.0, clock=time.monotonic):
        self.budget = float(budget)
        self.clock = clock
        self.end = clock() + self.budget

    def left(self):
        return max(0.0, self.end - self.clock())

    def expired(self):
        return self.left() <= 0.0

    def cap(self, seconds):
        return min(float(seconds), self.left())

    def finite(self):
        return self.budget != float("inf")
