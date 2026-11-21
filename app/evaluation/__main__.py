"""python -m app.evaluation — offline synthetic-dataset evaluation (Phase 13.0).

Runs the evaluation dataset through the real ContextBuilder / PromptBuilder with
no provider and no network. Multi-model comparison CLIs belong to Phase 13.4;
this entry point only evaluates the offline input contract.
"""
import argparse
import sys
from pathlib import Path
from uuid import uuid4

from app.evaluation import load_dataset, run_dataset, write_report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.evaluation",
        description="离线运行合成评估数据集（不调用任何模型、不访问网络）。")
    parser.add_argument("--dataset", required=True, help="数据集目录（含 dataset.json）")
    parser.add_argument("--output", default="evaluation-runs", help="报告输出根目录")
    parser.add_argument("--scenario", action="append", default=None,
                        help="只运行指定 scenario_id（可重复）")
    args = parser.parse_args(argv)

    dataset = load_dataset(args.dataset)
    run_id = str(uuid4())
    target = Path(args.output) / run_id
    result = run_dataset(dataset, work_dir=target / "work", run_id=run_id,
                         scenario_ids=tuple(args.scenario) if args.scenario else None)
    write_report(result, target)
    fails = result.total_failures
    print(f"run {run_id}: {len(result.scenario_results)} scenarios · "
          f"{fails} failures · {result.total_na} N/A")
    print(f"report: {target / 'report.md'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
