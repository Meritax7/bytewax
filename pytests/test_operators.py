import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from threading import Event

from pytest import fixture, raises

from bytewax.dataflow import Dataflow
from bytewax.execution import run_main, spawn_cluster, TestingEpochConfig
from bytewax.inputs import (
    ManualInputConfig,
    TestingBuilderInputConfig,
    TestingInputConfig,
)
from bytewax.outputs import ManualOutputConfig, TestingOutputConfig
from bytewax.recovery import SqliteRecoveryConfig
from bytewax.testing import TestingClock
from bytewax.window import TestingClockConfig, TumblingWindowConfig

# Stateful operators must test recovery to ensure serde works.
epoch_config = TestingEpochConfig()


@fixture
def recovery_config(tmp_path):
    yield SqliteRecoveryConfig(str(tmp_path))


def test_map():
    flow = Dataflow()

    inp = [0, 1, 2]
    flow.input("inp", TestingInputConfig(inp))

    def add_one(item):
        return item + 1

    flow.map(add_one)

    out = []
    flow.capture(TestingOutputConfig(out))

    run_main(flow)

    assert sorted(out) == sorted([1, 2, 3])


def test_flat_map():
    flow = Dataflow()

    inp = ["split this"]
    flow.input("inp", TestingInputConfig(inp))

    def split_into_words(sentence):
        return sentence.split()

    flow.flat_map(split_into_words)

    out = []
    flow.capture(TestingOutputConfig(out))

    run_main(flow)

    assert sorted(out) == sorted(["split", "this"])


def test_filter():
    flow = Dataflow()

    inp = [1, 2, 3]
    flow.input("inp", TestingInputConfig(inp))

    def is_odd(item):
        return item % 2 != 0

    flow.filter(is_odd)

    out = []
    flow.capture(TestingOutputConfig(out))

    run_main(flow)

    assert sorted(out) == sorted([1, 3])


def test_inspect():
    flow = Dataflow()

    inp = ["a"]
    flow.input("inp", TestingInputConfig(inp))

    seen = []
    flow.inspect(seen.append)

    out = []
    flow.capture(TestingOutputConfig(out))

    run_main(flow)

    assert sorted(out) == sorted(["a"])
    # Check side-effects after execution is complete.
    assert seen == sorted(["a"])


def test_inspect_epoch():
    flow = Dataflow()

    inp = ["a"]
    flow.input("inp", TestingInputConfig(inp))

    seen = []
    flow.inspect_epoch(lambda epoch, item: seen.append((epoch, item)))

    out = []
    flow.capture(TestingOutputConfig(out))

    run_main(flow)

    assert sorted(out) == sorted(["a"])
    # Check side-effects after execution is complete.
    assert seen == sorted([(0, "a")])


def test_reduce(recovery_config):
    flow = Dataflow()

    inp = [
        {"user": "a", "type": "login"},
        {"user": "a", "type": "post"},
        "BOOM",
        {"user": "b", "type": "login"},
        {"user": "a", "type": "logout"},
        {"user": "b", "type": "logout"},
    ]
    flow.input("inp", TestingInputConfig(inp))

    armed = Event()
    armed.set()

    def trigger(item):
        if item == "BOOM":
            if armed.is_set():
                raise RuntimeError("BOOM")
            else:
                return []
        else:
            return [item]

    flow.flat_map(trigger)

    def user_as_key(event):
        return (event["user"], [event])

    flow.map(user_as_key)

    def extend_session(session, event):
        return session + event

    def session_complete(session):
        return any(event["type"] == "logout" for event in session)

    flow.reduce("sessionizer", extend_session, session_complete)

    out = []
    flow.capture(TestingOutputConfig(out))

    with raises(RuntimeError):
        run_main(flow, epoch_config=epoch_config, recovery_config=recovery_config)

    assert sorted(out) == sorted([])

    # Disable bomb.
    armed.clear()
    out.clear()

    # Recover
    run_main(flow, epoch_config=epoch_config, recovery_config=recovery_config)

    assert sorted(out) == sorted(
        [
            (
                "a",
                [
                    {"user": "a", "type": "login"},
                    {"user": "a", "type": "post"},
                    {"user": "a", "type": "logout"},
                ],
            ),
            (
                "b",
                [
                    {"user": "b", "type": "login"},
                    {"user": "b", "type": "logout"},
                ],
            ),
        ]
    )


def test_integer_state_key_routes_to_worker(mp_ctx):
    with mp_ctx.Manager() as man:
        proc_count = 3
        worker_count_per_proc = 2
        worker_count = proc_count * worker_count_per_proc

        flow = Dataflow()

        # Every worker emits the entire range of (worker_index, 1)
        def input_builder(worker_index, worker_count, resume_state):
            assert resume_state is None
            for i in range(worker_count):
                yield None, (i, 1)

        flow.input("inp", ManualInputConfig(input_builder))

        flow.reduce("count", lambda x, s: s + x, lambda s: s >= worker_count)

        out = man.dict()
        for worker_index in range(worker_count):
            out[worker_index] = man.list()

        def output_builder(worker_index, worker_count):
            def output_handler(item):
                out[worker_index].append(item)

            return output_handler

        flow.capture(ManualOutputConfig(output_builder))

        spawn_cluster(
            flow, proc_count=proc_count, worker_count_per_proc=worker_count_per_proc
        )

        # Every worker should add up to the total number of
        # workers. We have to turn the manager versions of the data
        # structure back into the comparable versions.
        assert {k: list(v) for k, v in out.items()} == {
            worker_index: [(worker_index, worker_count)]
            for worker_index in range(worker_count)
        }


def test_stateful_map(recovery_config):
    flow = Dataflow()

    inp = [
        "a",
        "b",
        "BOOM",
        "b",
        "c",
    ]
    flow.input("inp", TestingInputConfig(inp))

    armed = Event()
    armed.set()

    def trigger(item):
        if item == "BOOM":
            if armed.is_set():
                raise RuntimeError("BOOM")
            else:
                return []
        else:
            return [item]

    flow.flat_map(trigger)

    def add_key(item):
        return item, item

    flow.map(add_key)

    def build_seen():
        return set()

    def check(seen, value):
        if value in seen:
            return seen, True
        else:
            seen.add(value)
            return seen, False

    flow.stateful_map("build_seen", build_seen, check)

    def remove_seen(key__is_seen):
        key, is_seen = key__is_seen
        if not is_seen:
            return [key]
        else:
            return []

    flow.flat_map(remove_seen)

    out = []
    flow.capture(TestingOutputConfig(out))

    with raises(RuntimeError):
        run_main(flow, epoch_config=epoch_config, recovery_config=recovery_config)

    assert sorted(out) == sorted(
        [
            "a",
            "b",
        ]
    )

    # Disable bomb
    armed.clear()
    out.clear()

    # Recover
    run_main(flow, epoch_config=epoch_config, recovery_config=recovery_config)

    assert sorted(out) == sorted(
        [
            "c",
        ]
    )


def test_stateful_map_error_on_non_kv_tuple():
    flow = Dataflow()

    inp = [
        {"user": "a", "type": "login"},
        {"user": "a", "type": "post"},
        {"user": "b", "type": "login"},
        {"user": "b", "type": "post"},
    ]
    flow.input("inp", TestingInputConfig(inp))

    def running_count(type_to_count, event):
        type_to_count[event["type"]] += 1
        current_count = type_to_count[event["type"]]
        return type_to_count, [(event["type"], current_count)]

    flow.stateful_map("running_count", lambda: defaultdict(int), running_count)

    out = []
    flow.capture(TestingOutputConfig(out))

    expect = (
        "Dataflow requires a `(key, value)` 2-tuple as input to every stateful "
        "operator for routing; got `{'user': 'a', 'type': 'login'}` instead"
    )

    with raises(TypeError, match=re.escape(expect)):
        run_main(flow)


def test_stateful_map_error_on_non_string_key():
    flow = Dataflow()

    # Note that the resulting key will be an int.
    inp = [
        {"user": {"id": 1}, "type": "login"},
        {"user": {"id": 1}, "type": "post"},
        {"user": {"id": 2}, "type": "login"},
        {"user": {"id": 2}, "type": "post"},
    ]
    flow.input("inp", TestingInputConfig(inp))

    def add_key(event):
        # Note that event["user"] is an entire dict, but keys must be
        # strings.
        return event["user"], event

    flow.map(add_key)

    def running_count(type_to_count, event):
        type_to_count[event["type"]] += 1
        current_count = type_to_count[event["type"]]
        return type_to_count, [(event["type"], current_count)]

    flow.stateful_map("running_count", lambda: defaultdict(int), running_count)

    out = []
    flow.capture(TestingOutputConfig(out))

    with raises(
        TypeError,
        match=re.escape(
            "Stateful logic functions must return string or integer keys in "
            "`(key, value)`; got `{'id': 1}` instead"
        ),
    ):
        run_main(flow)


def test_reduce_window(recovery_config):
    start_at = datetime(2022, 1, 1, tzinfo=timezone.utc)
    clock = TestingClock(start_at)

    flow = Dataflow()

    def gen():
        clock.now = start_at  # +0 sec; reset on recover
        yield ("ALL", 1)
        clock.now += timedelta(seconds=4)  # +4 sec
        yield ("ALL", 1)
        clock.now += timedelta(seconds=4)  # +8 sec
        yield "BOOM"
        yield ("ALL", 1)
        clock.now += timedelta(seconds=4)  # +12 sec
        yield ("ALL", 1)
        clock.now += timedelta(seconds=4)  # +16 sec

    flow.input("inp", TestingBuilderInputConfig(gen))

    armed = Event()
    armed.set()

    def trigger(item):
        if item == "BOOM":
            if armed.is_set():
                raise RuntimeError("BOOM")
            else:
                return []
        else:
            return [item]

    flow.flat_map(trigger)

    clock_config = TestingClockConfig(clock)
    window_config = TumblingWindowConfig(
        length=timedelta(seconds=10), start_at=start_at
    )

    def add(acc, x):
        return acc + x

    flow.reduce_window("add", clock_config, window_config, add)

    out = []
    flow.capture(TestingOutputConfig(out))

    with raises(RuntimeError):
        run_main(flow, epoch_config=epoch_config, recovery_config=recovery_config)

    # No windows yet after epoch 2.
    assert sorted(out) == sorted([])

    # Disable bomb
    armed.clear()
    out.clear()

    # Recover
    run_main(flow, epoch_config=epoch_config, recovery_config=recovery_config)

    # But it remembers the first two items in the first window.
    assert sorted(out) == sorted([("ALL", 3), ("ALL", 1)])


def test_fold_window(recovery_config):
    start_at = datetime(2022, 1, 1, tzinfo=timezone.utc)
    clock = TestingClock(start_at)

    flow = Dataflow()

    def gen():
        clock.now = start_at  # +0 sec; reset on recover
        yield {"user": "a", "type": "login"}  # Epoch 0
        clock.now += timedelta(seconds=4)  # +4 sec
        yield {"user": "a", "type": "post"}  # Epoch 1
        clock.now += timedelta(seconds=4)  # +8 sec
        yield {"user": "a", "type": "post"}  # Epoch 2
        clock.now += timedelta(seconds=4)  # +12 sec
        # First 10 sec window closes during processing this input.
        yield {"user": "b", "type": "login"}  # Epoch 3
        yield "BOOM"  # Epoch 4
        clock.now += timedelta(seconds=4)  # +16 sec
        yield {"user": "a", "type": "post"}  # Epoch 5
        clock.now += timedelta(seconds=4)  # +20 sec
        # Second 10 sec window closes during processing this input.
        yield {"user": "b", "type": "post"}  # Epoch 6
        clock.now += timedelta(seconds=4)  # +24 sec
        yield {"user": "b", "type": "post"}  # Epoch 7
        clock.now += timedelta(seconds=4)  # +28 sec

    flow.input("inp", TestingBuilderInputConfig(gen))

    armed = Event()
    armed.set()

    def trigger(item):
        if item == "BOOM":
            if armed.is_set():
                raise RuntimeError("BOOM")
            else:
                return []
        else:
            return [item]

    flow.flat_map(trigger)

    def key_off_user(event):
        return (event["user"], event["type"])

    flow.map(key_off_user)

    clock_config = TestingClockConfig(clock)
    window_config = TumblingWindowConfig(
        length=timedelta(seconds=10), start_at=start_at
    )

    def count(counts, typ):
        if typ not in counts:
            counts[typ] = 0
        counts[typ] += 1
        return counts

    flow.fold_window("count", clock_config, window_config, dict, count)

    out = []
    flow.capture(TestingOutputConfig(out))

    with raises(RuntimeError):
        run_main(flow, epoch_config=epoch_config, recovery_config=recovery_config)

    assert out == [
        ("a", {"login": 1, "post": 2}),
    ]

    # Disable bomb
    armed.clear()
    out.clear()

    # Recover
    run_main(flow, epoch_config=epoch_config, recovery_config=recovery_config)

    assert out == [
        # Output from epoch 3 is duplicated because the epoch would
        # only be closed and snapshotted for recovery on epoch 4, but
        # the exception during epoch 4 happens before the fold_window
        # operator gets to run. This is the best we can do in this
        # situation without figuring out a transactional exactly-once
        # kind of thing.
        ("a", {"login": 1, "post": 2}),
        ("b", {"login": 1}),
        ("a", {"post": 1}),
        ("b", {"post": 2}),
    ]
