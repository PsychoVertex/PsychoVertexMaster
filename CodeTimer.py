import time

class CodeTimer:
    def __init__(self, label="Time"):
        self.start = 0
        self.elapsed = 0
        self.label = label

    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.elapsed = time.perf_counter() - self.start
        print(f"[CODE TIMER] {self.label}: {int(self.elapsed * 1000)} ms")
