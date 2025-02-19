import argparse
import logging
from typing import Optional

import tensorflow as tf

from ..common.utils import get_git_revision_hash
from .config import create_runner, load_config

LOGGER = logging.getLogger(__package__ + ".train")

# As of TF 2.7, deterministic mode is slower, so we will disable it for now.
# os.environ["TF_DETERMINISTIC_OPS"] = "True"
# os.environ["TF_DISABLE_SEGMENT_REDUCTION_OP_DETERMINISM_EXCEPTIONS"] = "True"


def main() -> None:
    parser = argparse.ArgumentParser(description="Trains an NMT model")
    parser.add_argument("experiments", nargs="+", help="Experiment names")
    parser.add_argument("--mixed-precision", default=False, action="store_true", help="Enable mixed precision")
    parser.add_argument("--memory-growth", default=False, action="store_true", help="Enable memory growth")
    parser.add_argument("--num-devices", type=int, default=1, help="Number of devices to train on")
    parser.add_argument(
        "--eager-execution",
        default=False,
        action="store_true",
        help="Enable TensorFlow eager execution.",
    )
    args = parser.parse_args()

    rev_hash = get_git_revision_hash()

    if args.eager_execution:
        tf.config.run_functions_eagerly(True)
        tf.data.experimental.enable_debug_mode()

    for exp_name in args.experiments:
        config = load_config(exp_name)
        config.set_seed()
        runner = create_runner(config, mixed_precision=args.mixed_precision)
        runner.save_effective_config(str(config.exp_dir / f"effective-config-{rev_hash}.yml"), training=True)

        checkpoint_path: Optional[str] = None
        if not (config.exp_dir / "run").is_dir() and config.has_parent:
            checkpoint_path = str(config.exp_dir / "parent")

        print(f"=== Training ({exp_name}) ===")
        try:
            runner.train(num_devices=args.num_devices, with_eval=True, checkpoint_path=checkpoint_path)
        except RuntimeError as e:
            LOGGER.warning(str(e))
        print("Training completed")


if __name__ == "__main__":
    main()
