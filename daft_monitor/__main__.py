from daft_monitor.logging_setup import parse_bool
from daft_monitor.main import _parse_args, run_with_logging

args = _parse_args()
run_with_logging(
    config_path=args.config_path,
    run_once=args.once,
    environment=args.environment,
    log_level=args.log_level,
    write_logs=parse_bool(args.write_logs),
    log_dir=args.log_dir,
)
