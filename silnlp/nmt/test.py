import argparse
import logging
import random
import sys
from pathlib import Path
from typing import IO, Dict, Iterable, List, Optional, Set, Tuple, Union, cast, Sequence

import numpy as np
import sacrebleu
import tensorflow as tf
from machine.scripture import ORIGINAL_VERSIFICATION, VerseRef, book_number_to_id, get_books
from sacrebleu.metrics import BLEU, BLEUScore

from ..common.metrics import compute_meteor_score, compute_ter_score, compute_wer_score
from ..common.utils import get_git_revision_hash
from .config import Config, create_runner, load_config
from .utils import decode_sp, enable_memory_growth, get_best_model_dir, get_last_checkpoint

LOGGER = logging.getLogger(__name__)

_SUPPORTED_SCORERS = {"bleu", "sentencebleu", "chrf3", "meteor", "wer", "ter"}


class PairScore:
    def __init__(
        self,
        book: str,
        src_iso: str,
        trg_iso: str,
        bleu: Optional[BLEUScore],
        sent_len: int,
        projects: Set[str],
        other_scores: Dict[str, float] = {},
    ) -> None:
        self.src_iso = src_iso
        self.trg_iso = trg_iso
        self.bleu = bleu
        self.sent_len = sent_len
        self.num_refs = len(projects)
        self.refs = "_".join(sorted(projects))
        self.other_scores = other_scores
        self.book = book

    def writeHeader(self, file: IO) -> None:
        file.write("book,src_iso,trg_iso,num_refs,references,sent_len,scorer,score\n")

    def write(self, file: IO) -> None:
        if self.bleu is not None:
            file.write(f"{self.book},{self.src_iso},{self.trg_iso},{self.num_refs},{self.refs},{self.sent_len:d},")
            file.write(
                f"BLEU,{self.bleu.score:.2f}/{self.bleu.precisions[0]:.2f}/{self.bleu.precisions[1]:.2f}/"
                f"{self.bleu.precisions[2]:.2f}/{self.bleu.precisions[3]:.2f}/{self.bleu.bp:.3f}/"
                f"{self.bleu.sys_len:d}/{self.bleu.ref_len:d}\n"
            )
            tf.summary.scalar(f"{self.book}/BLEU", self.bleu.score)
        for key, val in self.other_scores.items():
            file.write(f"{self.book},{self.src_iso},{self.trg_iso},{self.num_refs},{self.refs},{self.sent_len:d},")
            file.write(f"{key},{val:.2f}\n")
            tf.summary.scalar(f"{self.book}/{key}", val)


def score_individual_books(
    book_dict: dict,
    src_iso: str,
    predictions_detok_path: str,
    scorers: Set[str],
    config: Config,
    ref_projects: Set[str],
):
    overall_sys: List[str] = []
    book_scores: List[PairScore] = []

    for book in book_dict.keys():
        for trg_iso, book_tuple in book_dict[book].items():
            pair_sys = book_tuple[0]
            pair_refs = book_tuple[1]
            overall_sys.extend(pair_sys)

            bleu_score = None
            if "bleu" in scorers:
                bleu_score = sacrebleu.corpus_bleu(
                    pair_sys,
                    pair_refs,
                    lowercase=True,
                    tokenize=config.data.get("sacrebleu_tokenize", "13a"),
                )

            if "sentencebleu" in scorers:
                write_sentence_bleu(
                    predictions_detok_path,
                    pair_sys,
                    pair_refs,
                    lowercase=True,
                    tokenize=config.data.get("sacrebleu_tokenize", "13a"),
                )

            other_scores: Dict[str, float] = {}
            if "chrf3" in scorers:
                chrf3_score = sacrebleu.corpus_chrf(pair_sys, pair_refs, char_order=6, beta=3, remove_whitespace=True)
                other_scores["CHRF3"] = np.round(float(chrf3_score.score), 2)

            if "meteor" in scorers:
                meteor_score = compute_meteor_score(trg_iso, pair_sys, pair_refs)
                if meteor_score is not None:
                    other_scores["METEOR"] = meteor_score

            if "wer" in scorers:
                wer_score = compute_wer_score(pair_sys, cast(List[str], pair_refs))
                if wer_score >= 0:
                    other_scores["WER"] = wer_score

            if "ter" in scorers:
                ter_score = compute_ter_score(pair_sys, pair_refs)
                if ter_score >= 0:
                    other_scores["TER"] = ter_score
            score = PairScore(book, src_iso, trg_iso, bleu_score, len(pair_sys), ref_projects, other_scores)
            book_scores.append(score)
    return book_scores


def process_individual_books(
    src_file_path: Path,
    pred_file_path: Path,
    ref_file_paths: List[Path],
    vref_file_path: Path,
    default_trg_iso: str,
    select_rand_ref_line: bool,
    books: Set[int],
):
    # Output data structure
    book_dict: Dict[str, dict] = {}
    ref_files = []

    try:
        # Get all references
        for ref_file_path in ref_file_paths:
            file = ref_file_path.open("r", encoding="utf-8")
            ref_files.append(file)

        with vref_file_path.open("r", encoding="utf-8") as vref_file, pred_file_path.open(
            "r", encoding="utf-8"
        ) as pred_file, src_file_path.open("r", encoding="utf-8") as src_file:
            for lines in zip(pred_file, vref_file, src_file, *ref_files):
                # Get file lines
                pred_line = lines[0].strip()
                detok_pred = decode_sp(pred_line)
                vref = lines[1].strip()
                src_line = lines[2].strip()
                # Get book
                if vref != "":
                    vref = VerseRef.from_string(vref.strip(), ORIGINAL_VERSIFICATION)
                    # Check if book in books
                    if vref.book_num in books:
                        # Get iso
                        book_iso = default_trg_iso
                        if src_line.startswith("<2"):
                            index = src_line.index(">")
                            val = src_line[2:index]
                            if val != "qaa":
                                book_iso = val
                        # If book not in dictionary add the book
                        if vref.book not in book_dict:
                            book_dict[vref.book] = {}
                        if book_iso not in book_dict[vref.book]:
                            book_dict[vref.book][book_iso] = ([], [])
                        book_pred, book_refs = book_dict[vref.book][book_iso]

                        # Add detokenized prediction to nested dictionary
                        book_pred.append(detok_pred)

                        # Check if random ref line selected or not
                        if select_rand_ref_line:
                            ref_index = random.randint(0, len(ref_files) - 1)
                            ref_line = lines[ref_index + 3].strip()
                            if len(book_refs) == 0:
                                book_refs.append([])
                            book_refs[0].append(ref_line)
                        else:
                            # For each reference text, add to book_refs
                            for ref_index in range(len(ref_files)):
                                ref_line = lines[ref_index + 3].strip()
                                if len(book_refs) == ref_index:
                                    book_refs.append([])
                                book_refs[ref_index].append(ref_line)
    finally:
        if ref_files is not None:
            for ref_file in ref_files:
                ref_file.close()
    return book_dict


def load_test_data(
    vref_file_name: str,
    src_file_name: str,
    pred_file_name: str,
    ref_pattern: str,
    output_file_name: str,
    ref_projects: Set[str],
    config: Config,
    books: Set[int],
    by_book: bool,
) -> Tuple[Dict[str, Tuple[List[str], List[List[str]]]], Dict[str, dict]]:
    dataset: Dict[str, Tuple[List[str], List[List[str]]]] = {}
    src_file_path = config.exp_dir / src_file_name
    pred_file_path = config.exp_dir / pred_file_name
    with src_file_path.open("r", encoding="utf-8") as src_file, pred_file_path.open(
        "r", encoding="utf-8"
    ) as pred_file, (config.exp_dir / output_file_name).open("w", encoding="utf-8") as out_file:
        ref_file_paths = list(config.exp_dir.glob(ref_pattern))
        select_rand_ref_line = False
        if len(ref_file_paths) > 1:
            if len(ref_projects) == 0:
                # no refs specified, so randomly select verses from all available train refs to build one ref
                select_rand_ref_line = True
                ref_file_paths = [p for p in ref_file_paths if config.is_train_project(p)]
            else:
                # use specified refs only
                ref_file_paths = [p for p in ref_file_paths if config.is_ref_project(ref_projects, p)]
        ref_files: List[IO] = []
        vref_file: Optional[IO] = None
        vref_file_path = config.exp_dir / vref_file_name
        if len(books) > 0 and vref_file_path.is_file():
            vref_file = vref_file_path.open("r", encoding="utf-8")
        try:
            for ref_file_path in ref_file_paths:
                ref_files.append(ref_file_path.open("r", encoding="utf-8"))
            default_trg_iso = config.default_trg_iso
            for lines in zip(src_file, pred_file, *ref_files):
                if vref_file is not None:
                    vref_line = vref_file.readline().strip()
                    if vref_line != "":
                        vref = VerseRef.from_string(vref_line, ORIGINAL_VERSIFICATION)
                        if vref.book_num not in books:
                            continue
                src_line = lines[0].strip()
                pred_line = lines[1].strip()
                detok_pred_line = decode_sp(pred_line)
                iso = default_trg_iso
                if src_line.startswith("<2"):
                    index = src_line.index(">")
                    val = src_line[2:index]
                    if val != "qaa":
                        iso = val
                if iso not in dataset:
                    dataset[iso] = ([], [])
                sys, refs = dataset[iso]
                sys.append(detok_pred_line)
                if select_rand_ref_line:
                    ref_lines: List[str] = [l for l in map(lambda l: l.strip(), lines[2:]) if len(l) > 0]
                    ref_index = random.randint(0, len(ref_lines) - 1)
                    ref_line = ref_lines[ref_index]
                    if len(refs) == 0:
                        refs.append([])
                    refs[0].append(ref_line)
                else:
                    for ref_index in range(len(ref_files)):
                        ref_line = lines[ref_index + 2].strip()
                        if len(refs) == ref_index:
                            refs.append([])
                        refs[ref_index].append(ref_line)
                out_file.write(detok_pred_line + "\n")
            book_dict: Dict[str, dict] = {}
            if by_book:
                book_dict = process_individual_books(
                    src_file_path,
                    pred_file_path,
                    ref_file_paths,
                    vref_file_path,
                    default_trg_iso,
                    select_rand_ref_line,
                    books,
                )
        finally:
            if vref_file is not None:
                vref_file.close()
            for ref_file in ref_files:
                ref_file.close()
    return dataset, book_dict


def sentence_bleu(
    hypothesis: str,
    references: List[str],
    smooth_method: str = "exp",
    smooth_value: float = None,
    lowercase: bool = False,
    tokenize: str = "13a",
    use_effective_order: bool = True,
) -> BLEUScore:
    """
    Substitute for the sacrebleu version of sentence_bleu, which uses settings that aren't consistent with
    the values we use for corpus_bleu, and isn't fully parameterized
    """
    metric = BLEU(
        smooth_method=smooth_method,
        smooth_value=smooth_value,
        force=False,
        lowercase=lowercase,
        tokenize=tokenize,
        effective_order=use_effective_order,
    )
    return metric.sentence_score(hypothesis, references)


def write_sentence_bleu(
    predictions_detok_path: str,
    preds: List[str],
    refs: List[List[str]],
    lowercase: bool = False,
    tokenize: str = "13a",
):
    scores_path = predictions_detok_path + ".scores.csv"
    with open(scores_path, "w", encoding="utf-8-sig") as scores_file:
        scores_file.write("Verse\tBLEU\t1-gram\t2-gram\t3-gram\t4-gram\tBP\tPrediction")
        for ref in refs:
            scores_file.write("\tReference")
        scores_file.write("\n")
        verse_num = 0
        for pred in preds:
            sentences: List[str] = []
            for ref in refs:
                sentences.append(ref[verse_num])
            bleu = sentence_bleu(pred, sentences, lowercase=lowercase, tokenize=tokenize)
            scores_file.write(
                f"{verse_num + 1}\t{bleu.score:.2f}\t{bleu.precisions[0]:.2f}\t{bleu.precisions[1]:.2f}\t"
                f"{bleu.precisions[2]:.2f}\t{bleu.precisions[3]:.2f}\t{bleu.bp:.3f}\t" + pred.rstrip("\n")
            )
            for sentence in sentences:
                scores_file.write("\t" + sentence.rstrip("\n"))
            scores_file.write("\n")
            verse_num += 1


def test_checkpoint(
    config: Config,
    force_infer: bool,
    by_book: bool,
    ref_projects: Set[str],
    checkpoint_path: Path,
    step: int,
    scorers: Set[str],
    books: Set[int],
) -> List[PairScore]:
    config.set_seed()
    vref_paths: List[str] = []
    features_file_names: List[str] = []
    predictions_file_names: List[str] = []
    refs_patterns: List[str] = []
    predictions_detok_file_names: List[str] = []
    suffix_str = "_".join(map(lambda n: book_number_to_id(n), sorted(books)))
    if len(suffix_str) > 0:
        suffix_str += "-"
    suffix_str += "avg" if step == -1 else str(step)

    features_file_name = "test.src.txt"
    if (config.exp_dir / features_file_name).is_file():
        # all test data is stored in a single file
        vref_paths.append("test.vref.txt")
        features_file_names.append(features_file_name)
        predictions_file_names.append(f"test.trg-predictions.txt.{suffix_str}")
        refs_patterns.append("test.trg.detok*.txt")
        predictions_detok_file_names.append(f"test.trg-predictions.detok.txt.{suffix_str}")
    else:
        # test data is split into separate files
        for src_iso in sorted(config.src_isos):
            for trg_iso in sorted(config.trg_isos):
                if src_iso == trg_iso:
                    continue
                prefix = f"test.{src_iso}.{trg_iso}"
                features_file_name = f"{prefix}.src.txt"
                if (config.exp_dir / features_file_name).is_file():
                    vref_paths.append(f"{prefix}.vref.txt")
                    features_file_names.append(features_file_name)
                    predictions_file_names.append(f"{prefix}.trg-predictions.txt.{suffix_str}")
                    refs_patterns.append(f"{prefix}.trg.detok*.txt")
                    predictions_detok_file_names.append(f"{prefix}.trg-predictions.detok.txt.{suffix_str}")

    checkpoint_name = "averaged checkpoint" if step == -1 else f"checkpoint {step}"

    features_paths: List[Union[str, List[str]]] = []
    predictions_paths: List[str] = []
    for i in range(len(predictions_file_names)):
        predictions_path = config.exp_dir / predictions_file_names[i]
        if force_infer or not predictions_path.is_file():
            features_path = config.exp_dir / features_file_names[i]
            vref_path = config.exp_dir / vref_paths[i]
            if vref_path.is_file():
                features_paths.append([str(features_path), str(vref_path)])
            else:
                features_paths.append(str(features_path))
            predictions_paths.append(str(predictions_path))
    if len(predictions_paths) > 0:
        runner = create_runner(config)
        print(f"Inferencing {checkpoint_name}...")
        runner.infer_multiple(features_paths, predictions_paths, checkpoint_path=str(checkpoint_path))

    print(f"Scoring {checkpoint_name}...")
    default_src_iso = config.default_src_iso
    scores: List[PairScore] = []
    overall_sys: List[str] = []
    overall_refs: List[List[str]] = []
    for vref_file_name, features_file_name, predictions_file_name, refs_pattern, predictions_detok_file_name in zip(
        vref_paths, features_file_names, predictions_file_names, refs_patterns, predictions_detok_file_names
    ):
        src_iso = default_src_iso
        if features_file_name != "test.src.txt":
            src_iso = features_file_name.split(".")[1]
        dataset, book_dict = load_test_data(
            vref_file_name,
            features_file_name,
            predictions_file_name,
            refs_pattern,
            predictions_detok_file_name,
            ref_projects,
            config,
            books,
            by_book,
        )

        for trg_iso, (pair_sys, pair_refs) in dataset.items():
            start_index = len(overall_sys)
            overall_sys.extend(pair_sys)
            for i, ref in enumerate(pair_refs):
                if i == len(overall_refs):
                    overall_refs.append([""] * start_index)
                overall_refs[i].extend(ref)
            # ensure that all refs are the same length as the sys
            for overall_ref in filter(lambda r: len(r) < len(overall_sys), overall_refs):
                overall_ref.extend([""] * (len(overall_sys) - len(overall_ref)))
            bleu_score = None
            if "bleu" in scorers:
                bleu_score = sacrebleu.corpus_bleu(
                    pair_sys,
                    cast(Sequence[Sequence[str]], pair_refs),
                    lowercase=True,
                    tokenize=config.data.get("sacrebleu_tokenize", "13a"),
                )

            if "sentencebleu" in scorers:
                write_sentence_bleu(
                    predictions_detok_file_name,
                    pair_sys,
                    cast(List[List[str]], pair_refs),
                    lowercase=True,
                    tokenize=config.data.get("sacrebleu_tokenize", "13a"),
                )

            other_scores: Dict[str, float] = {}
            if "chrf3" in scorers:
                chrf3_score = sacrebleu.corpus_chrf(
                    pair_sys, cast(Sequence[Sequence[str]], pair_refs), char_order=6, beta=3, remove_whitespace=True
                )
                other_scores["CHRF3"] = np.round(float(chrf3_score.score), 2)

            if "meteor" in scorers:
                meteor_score = compute_meteor_score(trg_iso, pair_sys, cast(List[Iterable[str]], pair_refs))
                if meteor_score is not None:
                    other_scores["METEOR"] = meteor_score

            if "wer" in scorers:
                wer_score = compute_wer_score(pair_sys, cast(List[str], pair_refs))
                if wer_score >= 0:
                    other_scores["WER"] = wer_score

            if "ter" in scorers:
                ter_score = compute_ter_score(pair_sys, cast(List[Iterable[str]], pair_refs))
                if ter_score >= 0:
                    other_scores["TER"] = ter_score

            scores.append(PairScore("ALL", src_iso, trg_iso, bleu_score, len(pair_sys), ref_projects, other_scores))
            if by_book is True:
                if len(book_dict) != 0:
                    book_scores = score_individual_books(
                        book_dict, src_iso, predictions_detok_file_name, scorers, config, ref_projects
                    )
                    scores.extend(book_scores)
                else:
                    print("Error: book_dict did not load correctly. Not scoring individual books.")
    if len(config.src_isos) > 1 or len(config.trg_isos) > 1:
        bleu = sacrebleu.corpus_bleu(overall_sys, cast(Sequence[Sequence[str]], overall_refs), lowercase=True)
        scores.append(PairScore("ALL", "ALL", "ALL", bleu, len(overall_sys), ref_projects))

    scores_file_root = f"scores-{suffix_str}"
    if len(ref_projects) > 0:
        ref_projects_suffix = "_".join(sorted(ref_projects))
        scores_file_root += f"-{ref_projects_suffix}"
    with (config.exp_dir / f"{scores_file_root}.csv").open("w", encoding="utf-8") as scores_file:
        if scores is not None:
            scores[0].writeHeader(scores_file)
        for results in scores:
            results.write(scores_file)
    return scores


def test(
    experiment: str,
    checkpoint: Optional[str] = None,
    last: bool = False,
    avg: bool = False,
    best: bool = False,
    force_infer: bool = False,
    scorers: Set[str] = set(),
    ref_projects: Set[str] = set(),
    books: Set[str] = set(),
    by_book: bool = False,
):
    exp_name = experiment
    config = load_config(exp_name)
    books_nums = get_books(list(books))

    if len(scorers) == 0:
        scorers.add("bleu")
    scorers.intersection_update(_SUPPORTED_SCORERS)

    best_model_path, best_step = get_best_model_dir(config.model_dir)
    results: Dict[int, List[PairScore]] = {}
    step: int
    if checkpoint is not None:
        checkpoint_path = config.model_dir / f"ckpt-{checkpoint}"
        step = int(checkpoint)
        results[step] = test_checkpoint(
            config,
            force_infer,
            by_book,
            ref_projects,
            checkpoint_path,
            step,
            scorers,
            books_nums,
        )

    if avg:
        try:
            checkpoint_path, _ = get_last_checkpoint(config.model_dir / "avg")
            step = -1
            results[step] = test_checkpoint(
                config,
                force_infer,
                by_book,
                ref_projects,
                checkpoint_path,
                step,
                scorers,
                books_nums,
            )
        except:
            LOGGER.info("No average checkpoint available.")

    if best:
        step = best_step
        if step not in results:
            checkpoint_path = best_model_path / "ckpt"
            results[step] = test_checkpoint(
                config,
                force_infer,
                by_book,
                ref_projects,
                checkpoint_path,
                step,
                scorers,
                books_nums,
            )

    if last or (not best and checkpoint is None and not avg):
        checkpoint_path, step = get_last_checkpoint(config.model_dir)

        if step not in results:
            results[step] = test_checkpoint(
                config,
                force_infer,
                by_book,
                ref_projects,
                checkpoint_path,
                step,
                scorers,
                books_nums,
            )

    for step in sorted(results.keys()):
        num_refs = results[step][0].num_refs
        if num_refs == 0:
            num_refs = 1
        checkpoint_name: str
        if step == -1:
            checkpoint_name = "averaged checkpoint"
        elif step == best_step:
            checkpoint_name = f"best checkpoint {step}"
        else:
            checkpoint_name = f"checkpoint {step}"
        books_str = "ALL" if len(books) == 0 else ", ".join(map(lambda n: book_number_to_id(n), sorted(books)))
        print(f"Test results for {checkpoint_name} ({num_refs} reference(s), books: {books_str})")
        for score in results[step]:
            score.write(sys.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description="Tests an NMT model")
    parser.add_argument("experiment", help="Experiment name")
    parser.add_argument("--memory-growth", default=False, action="store_true", help="Enable memory growth")
    parser.add_argument("--checkpoint", type=str, help="Test checkpoint")
    parser.add_argument("--last", default=False, action="store_true", help="Test last checkpoint")
    parser.add_argument("--best", default=False, action="store_true", help="Test best evaluated checkpoint")
    parser.add_argument("--avg", default=False, action="store_true", help="Test averaged checkpoint")
    parser.add_argument("--ref-projects", nargs="*", metavar="project", default=[], help="Reference projects")
    parser.add_argument("--force-infer", default=False, action="store_true", help="Force inferencing")
    parser.add_argument(
        "--scorers",
        nargs="*",
        metavar="scorer",
        choices=_SUPPORTED_SCORERS,
        default=[],
        help=f"List of scorers - {_SUPPORTED_SCORERS}",
    )
    parser.add_argument("--books", nargs="*", metavar="book", default=[], help="Books")
    parser.add_argument("--by-book", default=False, action="store_true", help="Score individual books")
    parser.add_argument(
        "--eager-execution",
        default=False,
        action="store_true",
        help="Enable TensorFlow eager execution.",
    )
    args = parser.parse_args()

    get_git_revision_hash()

    if args.eager_execution:
        tf.config.run_functions_eagerly(True)
        tf.data.experimental.enable_debug_mode()

    if args.memory_growth:
        enable_memory_growth()

    test(
        args.experiment,
        checkpoint=args.checkpoint,
        last=args.last,
        best=args.best,
        avg=args.avg,
        ref_projects=set(args.ref_projects),
        force_infer=args.force_infer,
        scorers=set(s.lower() for s in args.scorers),
        books=set(args.books),
        by_book=args.by_book,
    )


if __name__ == "__main__":
    main()
