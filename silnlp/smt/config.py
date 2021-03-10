import argparse
import os
import yaml

from ..common.utils import get_git_revision_hash, get_mt_exp_dir, merge_dict


_BASE_CONFIG: dict = {
    "model": "hmm",
    "seed": 111,
    "test_size": 250,
    "src_tokenizer": "latin",
    "trg_tokenizer": "latin",
}

_SUPPORTED_TOKENIZERS = {"whitespace", "latin", "zwsp"}


def load_config(exp_name: str) -> dict:
    root_dir = get_mt_exp_dir(exp_name)
    config_path = os.path.join(root_dir, "config.yml")

    config = _BASE_CONFIG.copy()

    with open(config_path, "r", encoding="utf-8") as file:
        loaded_config = yaml.safe_load(file)
        return merge_dict(config, loaded_config)


def main() -> None:
    parser = argparse.ArgumentParser(description="Creates a NMT experiment config file")
    parser.add_argument("experiment", help="Experiment name")
    parser.add_argument("--src-lang", type=str, required=True, help="Source language")
    parser.add_argument("--trg-lang", type=str, required=True, help="Target language")
    parser.add_argument("--src-tokenizer", type=str, required=False, help="Source language tokenizer")
    parser.add_argument("--trg-tokenizer", type=str, required=False, help="Target language tokenizer")
    parser.add_argument("--force", default=False, action="store_true", help="Overwrite existing config file")
    parser.add_argument("--seed", type=int, help="Randomization seed")
    parser.add_argument("--model", type=str, help="The word alignment model")
    args = parser.parse_args()

    print("Git commit:", get_git_revision_hash())

    root_dir = get_mt_exp_dir(args.experiment)
    config_path = os.path.join(root_dir, "config.yml")
    if os.path.isfile(config_path) and not args.force:
        print('The experiment config file already exists. Use "--force" if you want to overwrite the existing config.')
        return

    os.makedirs(root_dir, exist_ok=True)

    config = _BASE_CONFIG.copy()
    if args.model is not None:
        config["model"] = args.model
    config["src_lang"] = args.src_lang
    config["trg_lang"] = args.trg_lang
    if args.src_tokenizer is not None and args.src_tokenizer in _SUPPORTED_TOKENIZERS:
        config["src_tokenizer"] = args.src_tokenizer
    if args.trg_tokenizer is not None and args.trg_tokenizer in _SUPPORTED_TOKENIZERS:
        config["trg_tokenizer"] = args.trg_tokenizer
    if args.seed is not None:
        config["seed"] = args.seed
    with open(config_path, "w", encoding="utf-8") as file:
        yaml.dump(config, file)
    print("Config file created")


if __name__ == "__main__":
    main()
