import glob
from nlp.common.verse_ref import VerseRef
import os
import random
from typing import Iterable, List, Optional, Set, Tuple

import pandas as pd
from nltk.translate import Alignment

from nlp.alignment.config import get_aligner
from nlp.common.corpus import load_corpus


def compute_aer(alignments: Iterable[Alignment], references: Iterable[Alignment]) -> float:
    a_count, s_count, pa_count, sa_count = get_alignment_counts(alignments, references)
    if s_count + a_count == 0:
        return 0
    return 1 - ((pa_count + sa_count) / (s_count + a_count))


def compute_f_score(
    alignments: Iterable[Alignment], references: Iterable[Alignment], alpha: float = 0.5
) -> Tuple[float, float, float]:
    a_count, s_count, pa_count, sa_count = get_alignment_counts(alignments, references)
    precision = 1 if a_count == 0 else pa_count / a_count
    recall = 1 if s_count == 0 else sa_count / s_count
    f_score = 1 / ((alpha / precision) + ((1 - alpha) / recall))
    return (f_score, precision, recall)


def get_alignment_counts(alignments: Iterable[Alignment], references: Iterable[Alignment]) -> Tuple[int, int, int, int]:
    a_count = 0
    s_count = 0
    pa_count = 0
    sa_count = 0
    for alignment, reference in zip(alignments, references):
        a_count += len(alignment)
        for wp in reference:
            if len(wp) < 3 or wp[2]:
                s_count += 1
                if (wp[0], wp[1]) in alignment:
                    sa_count += 1
                    pa_count += 1
            elif (wp[0], wp[1]) in alignment:
                pa_count += 1
    return (a_count, s_count, pa_count, sa_count)


def load_alignments(input_file_path: str) -> List[Alignment]:
    alignments: List[Alignment] = []
    for line in load_corpus(input_file_path):
        if line.startswith("#"):
            continue
        alignments.append(Alignment.fromstring(line))
    return alignments


def load_vrefs(vref_file_path: str) -> List[VerseRef]:
    vrefs: List[VerseRef] = []
    for line in load_corpus(vref_file_path):
        vrefs.append(VerseRef.from_bbbcccvvv(int(line)))
    return vrefs


def filter_alignments_by_book(vrefs: List[VerseRef], alignments: List[Alignment], books: Set[int]) -> List[Alignment]:
    if len(books) == 0:
        return alignments

    results: List[Alignment] = []
    for vref, alignment in zip(vrefs, alignments):
        if vref.book_num in books:
            results.append(alignment)
    return results


def filter_alignments_by_index(alignments: List[Alignment], indices: List[int]) -> List[Alignment]:
    if len(indices) == 0:
        return alignments

    results: List[Alignment] = []
    for index in indices:
        results.append(alignments[index])
    return results


def compute_metrics(root_dir: str, books: Set[int] = set(), test_size: Optional[int] = None) -> pd.DataFrame:
    vref_file_path = os.path.join(root_dir, "refs.txt")
    vrefs = load_vrefs(vref_file_path)

    ref_file_path = os.path.join(root_dir, "alignments.gold.txt")
    references = load_alignments(ref_file_path)
    references = filter_alignments_by_book(vrefs, references, books)
    test_indices: List[int] = []
    if test_size is not None and len(references) > test_size:
        test_indices = random.sample(range(len(references)), test_size)
    references = filter_alignments_by_index(references, test_indices)

    aligner_names: List[str] = []
    aers: List[float] = []
    f_scores: List[float] = []
    precisions: List[float] = []
    recalls: List[float] = []
    for alignments_path in glob.glob(os.path.join(root_dir, "alignments.*.txt")):
        if alignments_path == ref_file_path:
            continue
        file_name = os.path.basename(alignments_path)
        parts = file_name.split(".")
        id = parts[1]
        aligner = get_aligner(id, root_dir)

        alignments = load_alignments(alignments_path)
        alignments = filter_alignments_by_book(vrefs, alignments, books)
        alignments = filter_alignments_by_index(alignments, test_indices)

        aer = compute_aer(alignments, references)
        f_score, precision, recall = compute_f_score(alignments, references)

        aligner_names.append(aligner.name)
        aers.append(aer)
        f_scores.append(f_score)
        precisions.append(precision)
        recalls.append(recall)

    return pd.DataFrame(
        {"AER": aers, "F-Score": f_scores, "Precision": precisions, "Recall": recalls},
        columns=["AER", "F-Score", "Precision", "Recall"],
        index=aligner_names,
    )
