import os
import uuid
from concurrent.futures import wait

from confluent_kafka import Producer
from confluent_kafka.admin import AdminClient, NewTopic
from pytest import fixture, mark, raises

from bytewax.connectors.kafka import KafkaInput
from bytewax.dataflow import Dataflow
from bytewax.execution import run_main
from bytewax.outputs import TestingOutputConfig

pytestmark = mark.skipif(
    "TEST_KAFKA_BROKER" not in os.environ,
    reason="No Kafka broker set via `TEST_KAFKA_BROKER` env var",
)
KAFKA_BROKER = os.environ.get("TEST_KAFKA_BROKER")


@fixture
def tmp_topic(request):
    config = {
        "bootstrap.servers": KAFKA_BROKER,
    }
    client = AdminClient(config)
    topic_name = f"pytest_{request.node.name}_{uuid.uuid4()}"
    wait(
        client.create_topics([NewTopic(topic_name, -1)], operation_timeout=5.0).values()
    )
    yield topic_name
    wait(client.delete_topics([topic_name], operation_timeout=5.0).values())


tmp_topic1 = tmp_topic
tmp_topic2 = tmp_topic


def test_kafka_input(tmp_topic1, tmp_topic2):
    config = {
        "bootstrap.servers": "localhost",
    }
    topics = [tmp_topic1, tmp_topic2]
    prod = Producer(config)
    for i, topic in enumerate(topics):
        for j in range(3):
            key = f"key-{i}-{j}".encode()
            value = f"value-{i}-{j}".encode()
            prod.produce(topic, value, key)
    prod.flush()

    flow = Dataflow()

    flow.input("inp", KafkaInput([KAFKA_BROKER], topics, tail=False))

    out = []
    flow.capture(TestingOutputConfig(out))

    run_main(flow)

    assert sorted(out) == [
        (b"key-0-0", b"value-0-0"),
        (b"key-0-1", b"value-0-1"),
        (b"key-0-2", b"value-0-2"),
        (b"key-1-0", b"value-1-0"),
        (b"key-1-1", b"value-1-1"),
        (b"key-1-2", b"value-1-2"),
    ]


def test_kafka_input_raises_on_topic_not_exist():
    flow = Dataflow()

    flow.input("inp", KafkaInput([KAFKA_BROKER], ["missing-topic"], tail=False))

    out = []
    flow.capture(TestingOutputConfig(out))

    with raises(Exception) as exinfo:
        run_main(flow)

    assert str(exinfo.value) == (
        "error listing partitions for Kafka topic `'missing-topic'`: "
        "Broker: Unknown topic or partition"
    )


def test_kafka_input_raises_on_str_brokers(tmp_topic):
    with raises(TypeError) as exinfo:
        KafkaInput(KAFKA_BROKER, [tmp_topic], tail=False)

    assert str(exinfo.value) == "brokers must be an iterable and not a string"


def test_kafka_input_raises_on_str_topics(tmp_topic):
    with raises(TypeError) as exinfo:
        KafkaInput([KAFKA_BROKER], tmp_topic, tail=False)

    assert str(exinfo.value) == "topics must be an iterable and not a string"
