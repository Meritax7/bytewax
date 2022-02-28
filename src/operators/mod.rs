use std::collections::HashMap;

use pyo3::prelude::*;

use crate::pyo3_extensions::{TdPyAny, TdPyCallable, TdPyIterator};
use crate::with_traceback;
use crate::log_func;

use log::debug;

// These are all shims which map the Timely Rust API into equivalent
// calls to Python functions through PyO3.

pub(crate) fn map(mapper: &TdPyCallable, item: TdPyAny) -> TdPyAny {
    debug!("{}, mapper:{:?}, item:{:?}", log_func!(), mapper, item);
    Python::with_gil(|py| with_traceback!(py, mapper.call1(py, (item,))).into())
}

pub(crate) fn flat_map(mapper: &TdPyCallable, item: TdPyAny) -> TdPyIterator {
    debug!("{}, mapper:{:?}, item:{:?}", log_func!(), mapper, item);
    Python::with_gil(|py| with_traceback!(py, mapper.call1(py, (item,))?.extract(py)))
}

pub(crate) fn filter(predicate: &TdPyCallable, item: &TdPyAny) -> bool {
    debug!("{}, predicate:{:?}, item:{:?}", log_func!(), predicate, item);
    Python::with_gil(|py| with_traceback!(py, predicate.call1(py, (item,))?.extract(py)))
}

pub(crate) fn inspect(inspector: &TdPyCallable, item: &TdPyAny) {
    debug!("{}, inspector:{:?}, item:{:?}", log_func!(), inspector, item);
    Python::with_gil(|py| {
        with_traceback!(py, inspector.call1(py, (item,)));
    });
}

pub(crate) fn inspect_epoch(inspector: &TdPyCallable, epoch: &u64, item: &TdPyAny) {
    debug!("{}, inspector:{:?}, item:{:?}", log_func!(), inspector, item);
    Python::with_gil(|py| {
        with_traceback!(py, inspector.call1(py, (*epoch, item)));
    });
}

pub(crate) fn reduce(
    reducer: &TdPyCallable,
    is_complete: &TdPyCallable,
    aggregator: &mut Option<TdPyAny>,
    key: &TdPyAny,
    value: TdPyAny,
) -> (bool, impl IntoIterator<Item = TdPyAny>) {
    Python::with_gil(|py| {
        debug!("{}, reducer:{:?}, key:{:?}, value:{:?}", log_func!(), reducer, key, value);
        let updated_aggregator = match aggregator {
            Some(aggregator) => {
                with_traceback!(py, reducer.call1(py, (aggregator.clone_ref(py), value))).into()
            }
            None => value,
        };
        let should_emit_and_discard_aggregator: bool = with_traceback!(
            py,
            is_complete
                .call1(py, (updated_aggregator.clone_ref(py),))?
                .extract(py)
        );

        *aggregator = Some(updated_aggregator.clone_ref(py));

        if should_emit_and_discard_aggregator {
            let emit = (key.clone_ref(py), updated_aggregator).to_object(py).into();
            debug!("{}, reducer:{:?}, key:{:?}, emit:{:?}, complete:{:?}", log_func!(), reducer, key, emit, should_emit_and_discard_aggregator);
            (true, Some(emit))
        } else {
            (false, None)
        }
    })
}

pub(crate) fn reduce_epoch(
    reducer: &TdPyCallable,
    aggregator: &mut Option<TdPyAny>,
    _key: &TdPyAny,
    value: TdPyAny,
) {
    debug!("{}, reducer:{:?}, key:{:?}, value:{:?}, agg:{:?}", log_func!(), reducer, _key, value, aggregator);
    Python::with_gil(|py| {
        let updated_aggregator = match aggregator {
            Some(aggregator) => {
                with_traceback!(py, reducer.call1(py, (aggregator.clone_ref(py), value))).into()
            }
            None => value,
        };
        *aggregator = Some(updated_aggregator);
    });
}

pub(crate) fn reduce_epoch_local(
    reducer: &TdPyCallable,
    aggregators: &mut HashMap<TdPyAny, TdPyAny>,
    all_key_value_in_epoch: &Vec<(TdPyAny, TdPyAny)>,
) {
    Python::with_gil(|py| {
        for (key, value) in all_key_value_in_epoch {
            debug!("{}, reducer:{:?}, key:{:?}, value:{:?}", log_func!(), reducer, key, value);
            aggregators
                .entry(key.clone_ref(py))
                .and_modify(|aggregator| {
                    *aggregator =
                        with_traceback!(py, reducer.call1(py, (aggregator.clone_ref(py), value)))
                            .into()
                })
                .or_insert(value.clone_ref(py));
        }
    });
}

pub(crate) fn stateful_map(
    mapper: &TdPyCallable,
    state: &mut TdPyAny,
    key: &TdPyAny,
    value: TdPyAny,
) -> (bool, impl IntoIterator<Item = TdPyAny>) {
    debug!("{}, mapper:{:?}, key:{:?}, value:{:?}", log_func!(), mapper, key, value);
    Python::with_gil(|py| {
        let (updated_state, emit_value): (TdPyAny, TdPyAny) = with_traceback!(
            py,
            mapper.call1(py, (state.clone_ref(py), value))?.extract(py)
        );
        debug!("{}, mapper:{:?}, key:{:?}, emit:{:?}", log_func!(), mapper, key, emit_value);

        *state = updated_state;

        let discard_state = Python::with_gil(|py| state.is_none(py));
        let emit = (key, emit_value).to_object(py).into();
        (discard_state, std::iter::once(emit))
    })
}

pub(crate) fn capture(captor: &TdPyCallable, epoch: &u64, item: &TdPyAny) {
    Python::with_gil(|py| with_traceback!(py, captor.call1(py, ((*epoch, item.clone_ref(py)),))));
}
