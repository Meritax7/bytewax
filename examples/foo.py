from bytewax import cluster_main, Dataflow
from bytewax.inputs import KafkaConfig

def output_builder(worker_index, worker_count):
    def output_fn(epoch_dataframe):
        print(epoch_dataframe)

    return output_fn

if __name__ == "__main__":
    input_config = KafkaConfig("localhost:9092", "foobar", "drivers")
    flow = Dataflow()
    flow.capture()
    cluster_main(
        flow,
        input_config,
        output_builder,
        [],  # addresses
        0,  # process id
    )
