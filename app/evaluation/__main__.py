"""python -m app.evaluation — evaluation entry point (Phase 13.0 / 13.4).

Three explicit modes; none runs automatically:

* default: dataset grounding evaluation (real ContextBuilder/PromptBuilder, no
  provider, no network);
* ``--models``: offline fixed-answer model comparison (no provider, no network);
* ``--online``: one explicitly selected live provider/model through the existing
  LLMGateway. It requires ``--provider`` and ``--confirm-online``; it never scans
  or calls every configured provider and never uses production workspace data
  (evaluation always materializes the synthetic dataset fixtures).
"""
import argparse
import sys
from pathlib import Path
from uuid import uuid4

from app.evaluation import load_dataset, run_dataset, write_report


def _real_gateway():
    from app.config.settings import AppConfig
    from app.services.llm import LLMConfigStore, LLMGateway
    config = AppConfig()
    return LLMGateway(LLMConfigStore.for_data_dir(config.storage_paths.data_dir))


def main(argv=None, *, gateway_factory=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.evaluation",
        description="评估入口：默认离线数据集评估；--models 离线模型对比；"
                    "--online 显式在线模型评估（需 --confirm-online）。")
    parser.add_argument("--dataset", required=True, help="数据集目录（含 dataset.json）")
    parser.add_argument("--output", default="evaluation-runs", help="报告输出根目录")
    parser.add_argument("--scenario", action="append", default=None,
                        help="只运行指定 scenario_id（可重复）")
    parser.add_argument("--models", default=None,
                        help="逗号分隔的固定答案模型标签（离线模型评估）")
    parser.add_argument("--online", action="store_true",
                        help="在线模型评估：真实调用已配置 Provider（需 --confirm-online）")
    parser.add_argument("--provider", default=None, help="在线评估使用的已配置 Provider 名称")
    parser.add_argument("--model", default=None,
                        help="可选的模型名；必须与已配置 Provider 的模型一致")
    parser.add_argument("--confirm-online", action="store_true",
                        help="明确确认允许真实网络调用")
    args = parser.parse_args(argv)

    scenario_ids = tuple(args.scenario) if args.scenario else None
    dataset = load_dataset(args.dataset)
    run_id = str(uuid4())
    target = Path(args.output) / run_id

    if args.online:
        if args.models:
            parser.error("--online 与 --models 不能同时使用。")
        if not args.provider:
            parser.error("在线评估必须显式指定 --provider。")
        if not args.confirm_online:
            parser.error("在线评估会真实调用模型；请加 --confirm-online 明确确认。")
        from app.evaluation.model_evaluation import run_online_model_evaluation
        from app.evaluation.report import write_model_report
        gateway = (gateway_factory or _real_gateway)()
        result = run_online_model_evaluation(
            dataset, gateway=gateway, provider_name=args.provider,
            model_name=args.model, work_dir=target / "work",
            scenario_ids=scenario_ids, run_id=run_id)
        write_model_report(result, target, dataset=dataset)
        failures = len(result.failures)
        print(f"online run {run_id}: provider {args.provider} · "
              f"{len(result.results)} calls · {failures} failures")
        print(f"report: {target / 'report.md'}")
        return 1 if failures else 0

    if args.models:
        from app.evaluation.model_evaluation import run_model_evaluation
        from app.evaluation.report import write_model_report
        labels = tuple(item.strip() for item in args.models.split(",") if item.strip())
        if not labels:
            parser.error("--models 需要至少一个模型标签。")
        result = run_model_evaluation(dataset, work_dir=target / "work",
                                      model_labels=labels, scenario_ids=scenario_ids,
                                      run_id=run_id)
        write_model_report(result, target, dataset=dataset)
        failures = len(result.failures)
        print(f"offline model run {run_id}: models {', '.join(labels)} · "
              f"{len(result.results)} answers · {failures} failures")
        print(f"report: {target / 'report.md'}")
        return 1 if failures else 0

    result = run_dataset(dataset, work_dir=target / "work", run_id=run_id,
                         scenario_ids=scenario_ids)
    write_report(result, target)
    fails = result.total_failures
    print(f"run {run_id}: {len(result.scenario_results)} scenarios · "
          f"{fails} failures · {result.total_na} N/A")
    print(f"report: {target / 'report.md'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
