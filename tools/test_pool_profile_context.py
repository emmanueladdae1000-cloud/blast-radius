"""A real reused spawn pool must set context before argument deserialization."""
import importlib
import multiprocessing
import os


def read_context(module_name):
    return importlib.import_module(module_name).ACTIVE[0]


class DuringUnpickle:
    def __init__(self, module_name):
        self.module_name = module_name

    def __reduce__(self):
        return read_context, (self.module_name,)


def identity(value):
    return value, os.getpid()


if __name__ == '__main__':
    from concurrent.futures import ProcessPoolExecutor
    module_name = os.environ['BR_TEST_PLUGIN']
    plugin = importlib.import_module(module_name)
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context('spawn')) as pool:
        plugin.ACTIVE[0] = 'first_outer_test'
        first, first_pid = pool.submit(identity, DuringUnpickle(module_name)).result(timeout=30)
        plugin.ACTIVE[0] = 'second_outer_test'
        second, second_pid = pool.submit(identity, DuringUnpickle(module_name)).result(timeout=30)
    assert first_pid == second_pid, 'The regression must reuse the same worker'
    assert first == 'first_outer_test', first
    assert second == 'second_outer_test', second
    from pathlib import Path
    assert list(Path(plugin.OUT).glob('*workerfinal.json.gz')), 'Late worker facts were not flushed'
    print(module_name, 'real shared-pool argument context and final flush verified')
