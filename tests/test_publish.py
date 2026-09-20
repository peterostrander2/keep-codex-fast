"""Controls for absent CI, idempotency, failures, and commit identity."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("publish", Path(__file__).resolve().parents[1] / "scripts/publish.py")
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)


def check():
    run = dict(id=1, head_sha="expected", html_url="fixture", status="completed", conclusion="success")
    def scenario(*, absent=False, prior=False, conclusion="success", moved=False, wrong=False):
        calls = []
        state = {"dispatch_attempted": prior}
        tick = [0]
        def request(path, dispatch=False):
            if dispatch:
                calls.append("dispatch")
                return None
            if "/git/ref/" in path:
                return {"object": {"sha": "other" if moved else "expected"}}
            result = dict(run, conclusion=conclusion)
            if wrong:
                result["head_sha"] = "other"
            return {"workflow_runs": [] if absent and not calls else [result]}
        def sleep(seconds):
            tick[0] += seconds
        try:
            result = publish.verify_ci("expected", state, lambda: None, request=request,
                                       sleep=sleep, clock=lambda: tick[0], timeout=30)
            return result, calls
        except (RuntimeError, TimeoutError) as exc:
            return exc, calls
    result, calls = scenario()
    assert isinstance(result, dict) and calls == []
    result, calls = scenario(absent=True)
    assert isinstance(result, dict) and calls == ["dispatch"]
    result, calls = scenario(absent=True, prior=True)
    assert isinstance(result, TimeoutError) and calls == []
    result, calls = scenario(conclusion="failure")
    assert isinstance(result, RuntimeError) and calls == []
    result, calls = scenario(moved=True)
    assert isinstance(result, RuntimeError) and calls == []
    result, calls = scenario(wrong=True)
    assert isinstance(result, TimeoutError) and calls == ["dispatch"]
    print("publish controls passed: 6 scenarios")


if __name__ == "__main__":
    check()
