"""Small independent checks for safe economic verification; no challenge answers."""
import difflib
import gzip
import json
import os
from pathlib import Path
import random
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import selector
import verify


def change(old, new, rel):
    diff = "diff --git a/{0} b/{0}\n".format(rel)
    diff += "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                      fromfile="a/" + rel, tofile="b/" + rel))
    return selector.parse_patch(diff)[0]


class CodeAndConfigTests(unittest.TestCase):
    def same(self, old, new):
        return verify.compiled_equivalent("example.py", old, compile(new.encode("utf-8"), "example.py", "exec"))

    def test_recursive_compiled_equivalence(self):
        self.assertTrue(self.same("def f():\n    return 1 + 2\n", "def f():\n    return 3\n"))
        self.assertFalse(self.same("def f():\n    return True\n", "def f():\n    return 1\n"))
        self.assertFalse(self.same("def f():\n    return (True,)\n", "def f():\n    return (1,)\n"))
        self.assertFalse(self.same("def f():\n    return 0.0\n", "def f():\n    return -0.0\n"))
        self.assertFalse(self.same("def f():\n    return 1e1000 - 1e1000\n", "def f():\n    return -(1e1000 - 1e1000)\n"))
        self.assertTrue(verify._plan_swaps("nan_example.py", "def f():\n    return 1e1000 - 1e1000\n",
                                          "def f():\n    return -(1e1000 - 1e1000)\n"))
        self.assertFalse(self.same("def f():\n    return lambda: 1\n", "def f():\n    return lambda: 2\n"))
        self.assertFalse(self.same("def f(x=1):\n    return x\n", "def f(x=2):\n    return x\n"))

    def config_same(self, old, new, rel):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, rel).write_text(old, encoding="utf-8")
            with mock.patch.object(selector, "SRC", directory):
                return selector.config_equivalent(rel, change(old, new, rel))

    def test_root_configuration_comments_and_real_values(self):
        old = '[tool.example]\nvalue = "# literal"\nitems = [1, 2]\n'
        self.assertTrue(self.config_same(old, "# explanation\n" + old, "pyproject.toml"))
        self.assertFalse(self.config_same(old, old.replace("# literal", "changed"), "pyproject.toml"))
        self.assertFalse(self.config_same(old, old.replace("[1, 2]", "[1, 3]"), "pyproject.toml"))
        self.assertFalse(self.config_same("[a]\nx=1\ny=2\n", "[a]\ny=2\nx=1\n", "pyproject.toml"))
        self.assertFalse(self.config_same("x=nan\n", "x=-nan\n", "pyproject.toml"))
        self.assertTrue(self.config_same("x = 1 + 2\n", "x = 3\n", "setup.py"))
        self.assertFalse(self.config_same("x = True\n", "x = 1\n", "setup.py"))
        self.assertFalse(self.config_same('x = "é"\n', '# coding: ascii\nx = "é"\n', "setup.py"))
        self.assertFalse(self.config_same('x = "é"\n', '# coding: latin-1\nx = "é"\n', "setup.py"))
        ini = "[pytest]\naddopts = -q\n"
        self.assertTrue(self.config_same(ini, "; explanation\n" + ini, "pytest.ini"))
        self.assertFalse(self.config_same(ini, ini.replace("-q", "-v"), "pytest.ini"))
        self.assertFalse(self.config_same("all:\n\techo hi\n", "# comment\nall:\n\techo hi\n", "Makefile"))

    def test_actual_selector_retains_encoding_cookie_changes_and_moves(self):
        maps = types.SimpleNamespace(all={0, 1, 2}, src=lambda rel: {"strs": []})
        cases = [
            ('value = "é"\n', '# coding: latin-1\nvalue = "é"\n'),
            ('value = "é"\n', '# coding: ascii\nvalue = "é"\n'),
            ('# header\n# coding: latin-1\nvalue = "é"\n',
             '# inserted\n# header\n# coding: latin-1\nvalue = "é"\n'),
            ('# header\n# placeholder\n# coding: latin-1\nvalue = "é"\n',
             '# header\n# coding: latin-1\nvalue = "é"\n'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "sqlglot").mkdir()
            rel = "sqlglot/example.py"
            with mock.patch.object(selector, "SRC", directory):
                for old, new in cases:
                    Path(directory, rel).write_text(old, encoding="utf-8")
                    selected, reason = selector.Selector(maps).py_change(rel, change(old, new, rel))
                    self.assertEqual(selected, maps.all)
                    self.assertEqual(reason, "encoding-all")


class EconomicTests(unittest.TestCase):
    def costs(self, ids, warm, setup=None):
        result = selector.VerificationCosts("/missing-cost-facts.json")
        result.tests = dict(zip(ids, warm))
        result.classes = setup or {}
        return result

    def test_complete99_probe_estimate_has_margin_over_measured_cpu(self):
        with gzip.open(ROOT / "tpcds_lines.json.gz", "rt") as source:
            query_ms = json.load(source)["ms"]
        self.assertEqual(len(query_ms), 99)
        self.assertEqual(sum(query_ms), 15436)
        with mock.patch.object(selector, "TPC_FIXED_MS", 1400):
            estimate = selector.tpcds_probe_estimate(range(99), query_ms)
        self.assertEqual(estimate, 19370)
        self.assertGreater(estimate, 17440)  # measured total CPU, parent+child
        marginal_alone = selector.pack_cost({55}, {55: 21454})
        self.assertEqual(marginal_alone, 23454)
        self.assertLess(1.1 * estimate, marginal_alone)

    def test_unknown_query_timing_uses_conservative_fallback(self):
        with mock.patch.object(selector, "TPC_FIXED_MS", 1400), \
             mock.patch.object(selector, "TPC_QUERY_MS", 260):
            self.assertEqual(selector.tpcds_probe_estimate([0, 1], [100]), 1816)

    def test_verification_cpu_budget_never_exceeds_expected_benefit_or5s(self):
        self.assertAlmostEqual(selector.verification_cpu_budget(186, 2028), 0.2546)
        self.assertEqual(selector.verification_cpu_budget(4714, 6686), 5.0)
        self.assertEqual(selector.verification_cpu_budget(2000, 2100), 2.1)

    def test_cost_only_packing_matches_emitted_worker_cost(self):
        rng = random.Random(421)
        for _ in range(100):
            recorded = {i: rng.randrange(0, 10000) for i in range(rng.randrange(0, 100))}
            workers = selector.pack(list(recorded.items()))
            actual = max((sum(recorded[t] for t in worker) for worker in workers), default=0)
            actual += 2000 * len(workers)
            self.assertEqual(selector.pack_cost(recorded, recorded), actual)

    def test_large_cheap_set_and_nonconsecutive_official_numbers(self):
        ids = [f"tests/test_example.py::Example::test_{i}" for i in range(200)]
        numbers = {tid: 1000 + 3 * i for i, tid in enumerate(ids)}
        recorded = {number: 4 for number in numbers.values()}
        plan = self.costs(ids, [1] * 200).subset(set(range(200)), ids, numbers, recorded)
        self.assertIsNotNone(plan)
        self.assertEqual(set(plan[0]), set(range(200)))
        self.assertGreater(plan[2], plan[1])

    def test_unknown_and_unprofitable_tests_stay_selected(self):
        ids = [f"tests/test_example.py::Example::test_{i}" for i in range(150)]
        ids += ["tests/test_unknown.py::Unknown::test_full", "tests/test_scope.py::Scope::test_full"]
        numbers = {tid: 500 + 2 * i for i, tid in enumerate(ids)}
        recorded = {number: 10 for number in numbers.values()}
        recorded[numbers[ids[-2]]] = 100
        recorded[numbers[ids[-1]]] = 2533
        costs = self.costs(ids[:150] + [ids[-1]], [1] * 150 + [3715])
        plan = costs.subset(set(range(len(ids))), ids, numbers, recorded)
        self.assertIsNotNone(plan)
        self.assertEqual(set(plan[0]), set(range(150)))
        retained = set(numbers.values()) - {numbers[ids[t]] for t in plan[0]}
        self.assertIn(numbers[ids[-2]], retained)
        self.assertIn(numbers[ids[-1]], retained)

    def test_cheap_work_is_skipped_when_packing_saves_nothing(self):
        ids = [f"tests/test_example.py::Example::test_{i}" for i in range(150)]
        ids += ["tests/test_unknown.py::Unknown::test_full", "tests/test_unknown.py::Unknown::test_other"]
        numbers = {tid: i for i, tid in enumerate(ids)}
        recorded = {i: 10 for i in range(150)}
        recorded[150] = 10000
        recorded[151] = 2533
        self.assertIsNone(self.costs(ids[:150], [1] * 150).subset(set(range(152)), ids, numbers, recorded))

    def test_actual_full_tpch_can_be_economical_including_setup(self):
        tid = "tests/test_executor.py::TestExecutor::test_execute_tpch"
        costs = self.costs([tid], [112], {"tests/test_executor.py::TestExecutor": 1500})
        plan = costs.subset({0}, [tid], {tid: 88}, {88: 3863})
        self.assertEqual(plan[0], [0])
        self.assertGreater(plan[1], 2000)  # setup was charged, not assumed warm
        costs.classes["tests/test_executor.py::TestExecutor"] = 10000
        self.assertIsNone(costs.subset({0}, [tid], {tid: 88}, {88: 3863}))

    def test_fresh_interpreter_tests_never_verified_in_process(self):
        tid = next(iter(selector.NO_VERIFY))
        self.assertIsNone(self.costs([tid], [1]).subset({0}, [tid], {tid: 1}, {1: 10000}))


class ExecutionTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "fork"), "requires Linux CPU timer")
    def test_cpu_termination_discards_even_preceding_passed_methods(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "economic_busy.py")
            old = "def answer():\n    return 1\n"
            new = "def answer():\n    while True:\n        pass\n"
            path.write_text(old)
            module = types.ModuleType("economic_busy")
            exec(compile(old, str(path), "exec"), module.__dict__)
            marker = Path(directory, "first-method-completed")
            class Example(unittest.TestCase):
                def test_first(self):
                    marker.write_text("passed")
                def test_busy(self):
                    module.answer()
            tests = types.SimpleNamespace(Example=Example)
            ids = ["economic_cpu_test.py::Example::test_first", "economic_cpu_test.py::Example::test_busy"]
            jobs = [(88, {path.name: new}, [0, 1], 4.0, 0.03)]
            before = time.monotonic()
            with mock.patch.dict(sys.modules, {"economic_busy": module, "economic_cpu_test": tests}), \
                 mock.patch.object(verify, "_setup"):
                result = verify.run(jobs, ids, directory, time.time() + 5, lambda *args: None)
            self.assertEqual(result, {})  # no partial passing prefix accepted
            self.assertTrue(marker.exists())
            self.assertLess(time.monotonic() - before, 2.0)

    @unittest.skipUnless(hasattr(os, "fork"), "requires Linux CPU timer")
    def test_fresh_cpu_termination_discards_all_partial_results(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory, "fresh-first-completed")
            class Example(unittest.TestCase):
                def test_first(self):
                    marker.write_text("passed")
                def test_busy(self):
                    while True:
                        pass
            tests = types.SimpleNamespace(Example=Example)
            ids = ["economic_fresh_cpu_test.py::Example::test_first", "economic_fresh_cpu_test.py::Example::test_busy"]
            jobs = [(89, {}, [0, 1], 4.0, 0.03)]
            with mock.patch.dict(sys.modules, {"economic_fresh_cpu_test": tests}), \
                 mock.patch.object(verify, "_setup"), mock.patch.object(verify, "_install_finder", return_value={}), \
                 mock.patch.object(verify, "_import_patched"):
                result = verify.run_fresh(jobs, ids, directory, time.time() + 5, lambda *args: None)
            self.assertEqual(result, {})
            self.assertTrue(marker.exists())

    def test_failure_skip_and_every_unrun_test_are_retained(self):
        class Example(unittest.TestCase):
            def test_pass(self):
                pass
            def test_fail(self):
                self.fail("expected synthetic failure")
            @unittest.skip("synthetic unverified method")
            def test_skip(self):
                pass
        module = types.SimpleNamespace(Example=Example)
        ids = [f"test_a.py::Example::{name}" for name in ("test_pass", "test_fail", "test_skip")]
        self.assertEqual(verify._run_tests([0, 1, 2], ids, {"test_a.py": module}), ([1], [2]))
        self.assertEqual(verify._run_tests([0, 2, 1], ids, {"test_a.py": module}), ([], [2, 1]))
        self.assertEqual(verify._run_tests([0, 1], ids, {}), ([], [0, 1]))

    def test_same_named_classes_each_receive_setup(self):
        calls = []
        def example(label):
            class Example(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    calls.append(label)
                def test_pass(self):
                    pass
            return types.SimpleNamespace(Example=Example)
        ids = ["test_a.py::Example::test_pass", "test_b.py::Example::test_pass"]
        self.assertEqual(verify._run_tests([0, 1], ids, {"test_a.py": example("a"), "test_b.py": example("b")}), ([], []))
        self.assertEqual(calls, ["a", "b"])

    @unittest.skipUnless(hasattr(os, "fork"), "requires Linux fork")
    def test_swap_plans_are_created_in_parent_and_baseline_compile_shared(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "economic_fake.py")
            old = "def answer():\n    return 1\n"
            path.write_text(old)
            module = types.ModuleType("economic_fake")
            exec(compile(old, str(path), "exec"), module.__dict__)
            class Example(unittest.TestCase):
                def test_original_answer(self):
                    self.assertEqual(module.answer(), 1)
            tests = types.SimpleNamespace(Example=Example)
            jobs = [(i, {path.name: old.replace("return 1", f"return {i+2}")}, [0], 4) for i in range(2)]
            planned_in = []
            original_plan = verify._plan_swaps
            def plan(*args):
                planned_in.append(os.getpid())
                return original_plan(*args)
            verify._old_code_tree.cache_clear()
            messages = []
            with mock.patch.dict(sys.modules, {"economic_fake": module, "economic_test": tests}), \
                 mock.patch.object(verify, "_setup"), mock.patch.object(verify, "_plan_swaps", side_effect=plan), \
                 mock.patch.object(verify, "_children_cpu", side_effect=[100.0, 100.015, 100.025]):
                result = verify.run(jobs, ["economic_test.py::Example::test_original_answer"], directory,
                                    time.time() + 10, lambda *args: messages.append(" ".join(map(str, args))))
            self.assertEqual(result, {0: ([0], []), 1: ([0], [])})
            self.assertEqual(planned_in, [os.getpid(), os.getpid()])
            self.assertEqual(verify._old_code_tree.cache_info().misses, 1)
            self.assertEqual(module.answer(), 1)  # fork swaps did not leak to parent
            self.assertIn("verify: job 0 cpu 0.015", messages)
            self.assertIn("verify: job 1 cpu 0.010", messages)


if __name__ == "__main__":
    unittest.main(verbosity=2)
