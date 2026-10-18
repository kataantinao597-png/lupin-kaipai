"""评估框架（代码层）。

用固定用例集 + mock transport 跑评估，不需要真实 Key。
真实模型评估：运维用真实 AIService 替换 mock 即可（见 run_eval 的 transport 参数）。
"""
from .cases import EvalCase, FIXED_CASES
from .runner import EvalReport, run_eval

__all__ = ["EvalCase", "FIXED_CASES", "EvalReport", "run_eval"]
