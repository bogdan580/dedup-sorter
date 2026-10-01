"""Tests for progress.py's ProgressCounter."""
import threading

from tidy.progress import DEFAULT_WORKERS, ProgressCounter


class TestProgressCounter:
    def test_starts_at_zero_zero(self):
        counter = ProgressCounter()
        assert counter.snapshot() == (0, 0)

    def test_set_total_replaces_total_not_done(self):
        counter = ProgressCounter()
        counter.increment(3)
        counter.set_total(10)
        assert counter.snapshot() == (3, 10)

    def test_add_total_accumulates(self):
        counter = ProgressCounter()
        counter.set_total(5)
        counter.add_total(7)
        assert counter.snapshot() == (0, 12)

    def test_increment_default_is_one(self):
        counter = ProgressCounter()
        counter.increment()
        counter.increment()
        assert counter.snapshot() == (2, 0)

    def test_increment_by_delta(self):
        counter = ProgressCounter()
        counter.increment(4)
        assert counter.snapshot() == (4, 0)

    def test_concurrent_increments_from_many_threads_lose_nothing(self):
        # The whole point of the lock: 50 threads each incrementing 100
        # times must land on exactly 5000, not something short due to a
        # lost update race.
        counter = ProgressCounter()

        def worker():
            for _ in range(100):
                counter.increment()

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert counter.snapshot() == (5000, 0)

    def test_default_workers_is_four(self):
        assert DEFAULT_WORKERS == 4


class TestPhaseCounter:
    """The placement bar is fed by several engine calls in
    a row, each of which opens with set_total(len(its own candidates)). A
    phase is what stops the later ones from wiping the earlier ones' work."""

    def test_phase_set_total_adds_to_parent_instead_of_replacing(self):
        counter = ProgressCounter()
        counter.phase().set_total(4)
        counter.phase().set_total(6)
        assert counter.snapshot() == (0, 10)

    def test_declared_share_is_not_added_twice(self):
        # The placement path: the caller declares the whole job up front,
        # then each engine call declares its own share again. The total must
        # come out as declared once, not twice.
        counter = ProgressCounter()
        counter.add_total(10)
        counter.phase(declared=4).set_total(4)
        counter.phase(declared=6).set_total(6)
        assert counter.snapshot() == (0, 10)

    def test_engine_declaring_more_than_expected_grows_the_total(self):
        counter = ProgressCounter()
        counter.add_total(10)
        counter.phase(declared=4).set_total(7)
        assert counter.snapshot() == (0, 13)

    def test_repeated_set_total_on_one_phase_is_not_cumulative(self):
        counter = ProgressCounter()
        phase = counter.phase()
        phase.set_total(5)
        phase.set_total(5)
        phase.set_total(8)
        assert counter.snapshot() == (0, 8)

    def test_increment_goes_straight_to_the_parent(self):
        counter = ProgressCounter()
        phase = counter.phase()
        phase.set_total(3)
        phase.increment()
        phase.increment(2)
        assert counter.snapshot() == (3, 3)

    def test_add_total_on_a_phase_reaches_the_parent(self):
        counter = ProgressCounter()
        phase = counter.phase(declared=2)
        phase.add_total(3)
        # add_total() raised this phase's share to 5, so an engine that then
        # announces "5 items" must add nothing on top.
        phase.set_total(5)
        assert counter.snapshot() == (0, 3)

    def test_snapshot_reads_the_parent(self):
        counter = ProgressCounter()
        counter.set_total(9)
        counter.increment(2)
        assert counter.phase().snapshot() == (2, 9)

    def test_concurrent_phases_lose_nothing(self):
        counter = ProgressCounter()

        def worker():
            phase = counter.phase()
            phase.set_total(10)
            for _ in range(10):
                phase.increment()

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert counter.snapshot() == (200, 200)
