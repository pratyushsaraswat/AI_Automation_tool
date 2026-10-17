import random
import time


class Human:
    """Paces actions so the tool behaves like a person, not a burst of requests."""

    def __init__(self, delays: dict):
        self.action = tuple(delays.get("action", [0.6, 1.8]))
        self.gap = tuple(delays.get("between_jobs", [25, 70]))

    def pause(self):
        time.sleep(random.uniform(*self.action))

    def between_jobs(self):
        secs = random.uniform(*self.gap)
        print(f"  waiting {secs:.0f}s before next job...")
        time.sleep(secs)

    def type(self, locator, text: str):
        locator.click()
        self.pause()
        locator.press_sequentially(text, delay=random.randint(40, 110))
