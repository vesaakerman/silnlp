import itertools
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple, Union

import pandas as pd
from machine.corpora import TextFileTextCorpus
from machine.scripture import ORIGINAL_VERSIFICATION, VerseRef
from machine.tokenization import LatinWordTokenizer
from sklearn.model_selection import train_test_split

from .environment import SIL_NLP_ENV


def write_corpus(corpus_path: Path, sentences: Iterable[str], append: bool = False) -> None:
    with corpus_path.open("a" if append else "w", encoding="utf-8", newline="\n") as file:
        for sentence in sentences:
            file.write(sentence + "\n")


def load_corpus(corpus_path: Path) -> Iterator[str]:
    with corpus_path.open("r", encoding="utf-8-sig") as in_file:
        for line in in_file:
            line = line.strip()
            yield line


def tokenize_corpus(input_path: Path, output_path: Path) -> None:
    corpus = TextFileTextCorpus(input_path).tokenize(LatinWordTokenizer()).escape_spaces().nfc_normalize().lowercase()
    with output_path.open("w", encoding="utf-8", newline="\n") as output_stream, corpus.get_rows() as rows:
        for row in rows:
            output_stream.write(row.text + "\n")


def get_scripture_parallel_corpus(
    src_file_path: Path, trg_file_path: Path, remove_empty_sentences: bool = True
) -> pd.DataFrame:
    vrefs: List[VerseRef] = []
    src_sentences: List[str] = []
    trg_sentences: List[str] = []
    indices: List[int] = []
    with (SIL_NLP_ENV.assets_dir / "vref.txt").open("r", encoding="utf-8") as vref_file, src_file_path.open(
        "r", encoding="utf-8"
    ) as src_file, trg_file_path.open("r", encoding="utf-8") as trg_file:
        index = 0
        for vref_line, src_line, trg_line in zip(vref_file, src_file, trg_file):
            vref_line = vref_line.strip()
            src_line = src_line.strip()
            trg_line = trg_line.strip()
            vref = VerseRef.from_string(vref_line, ORIGINAL_VERSIFICATION)
            if src_line == "<range>" and trg_line == "<range>":
                if vref.chapter_num == vrefs[-1].chapter_num:
                    vrefs[-1].simplify()
                    vrefs[-1] = VerseRef.from_range(vrefs[-1], vref)
            elif src_line == "<range>":
                if vref.chapter_num == vrefs[-1].chapter_num:
                    vrefs[-1].simplify()
                    vrefs[-1] = VerseRef.from_range(vrefs[-1], vref)
                if len(trg_line) > 0:
                    if len(trg_sentences[-1]) > 0:
                        trg_sentences[-1] += " "
                    trg_sentences[-1] += trg_line
            elif trg_line == "<range>":
                if vref.chapter_num == vrefs[-1].chapter_num:
                    vrefs[-1].simplify()
                    vrefs[-1] = VerseRef.from_range(vrefs[-1], vref)
                if len(src_line) > 0:
                    if len(src_sentences[-1]) > 0:
                        src_sentences[-1] += " "
                    src_sentences[-1] += src_line
            else:
                vrefs.append(vref)
                src_sentences.append(src_line)
                trg_sentences.append(trg_line)
                indices.append(index)
            index += 1

    if remove_empty_sentences:
        for i in range(len(vrefs) - 1, -1, -1):
            if len(src_sentences[i]) == 0 or len(trg_sentences[i]) == 0:
                vrefs.pop(i)
                src_sentences.pop(i)
                trg_sentences.pop(i)
                indices.pop(i)
    else:
        for i in range(len(vrefs) - 1, -1, -1):
            if len(src_sentences[i]) == 0 or len(trg_sentences[i]) == 0:
                src_sentences[i] = ""
                trg_sentences[i] = ""

    data = {"vref": vrefs, "source": src_sentences, "target": trg_sentences}
    return pd.DataFrame(data, index=indices)


def get_mt_corpus_path(corpus: str) -> Path:
    corpus_path = SIL_NLP_ENV.mt_corpora_dir / f"{corpus}.txt"
    if corpus_path.is_file():
        return corpus_path
    return SIL_NLP_ENV.mt_scripture_dir / f"{corpus}.txt"


def split_parallel_corpus(
    corpus: pd.DataFrame, split_size: Union[float, int], split_indices: Set[int] = None
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    split: pd.DataFrame
    if split_indices is None:
        if split_size == 0:
            split = pd.DataFrame(columns=corpus.columns)
        elif split_size >= len(corpus):
            split = corpus
            corpus = pd.DataFrame(columns=corpus.columns)
        else:
            corpus, split = train_test_split(corpus, test_size=split_size)
            corpus = corpus.copy()
            split = split.copy()
    else:
        split = corpus.filter(split_indices, axis=0)
        corpus.drop(split_indices, inplace=True, errors="ignore")
    return corpus, split


def filter_parallel_corpus(corpus: pd.DataFrame, score_threshold: float) -> pd.DataFrame:
    if score_threshold < 1:
        # Filter the corpus entries with alignment scores less than the threshold
        score_threshold = min(corpus["score"].quantile(0.1), score_threshold)
        return corpus[corpus["score"] > score_threshold]
    elif score_threshold < len(corpus):
        # Filter <n> corpus entries with the lowest alignment scores (n = score_threshold)
        return corpus.sort_values("score").iloc[int(score_threshold) :]

    return corpus


def split_corpus(corpus_size: int, split_size: Union[float, int], used_indices: Set[int] = set()) -> Optional[Set[int]]:
    if isinstance(split_size, float):
        split_size = int(split_size if split_size > 1 else corpus_size * split_size)
    population = (
        range(corpus_size) if len(used_indices) == 0 else [i for i in range(corpus_size) if i not in used_indices]
    )
    if split_size >= len(population):
        return None

    return set(random.sample(population, split_size))


def get_scripture_path(iso: str, project: str) -> Path:
    return SIL_NLP_ENV.mt_scripture_dir / f"{iso}-{project}.txt"


def parse_scripture_path(data_file_path: Path) -> Tuple[str, str]:
    file_name = data_file_path.stem
    parts = file_name.split("-")
    return parts[0], parts[1]


def include_books(corpus: pd.DataFrame, books: Set[int]) -> pd.DataFrame:
    return corpus[corpus.apply(lambda r: r["vref"].book_num in books, axis=1)].copy()


def exclude_books(corpus: pd.DataFrame, books: Set[int]) -> pd.DataFrame:
    return corpus[corpus.apply(lambda r: r["vref"].book_num not in books, axis=1)].copy()


def get_terms_metadata_path(list_name: str, mt_terms_dir: Path = SIL_NLP_ENV.mt_terms_dir) -> Path:
    md_path = SIL_NLP_ENV.assets_dir / f"{list_name}-metadata.txt"
    if md_path.is_file():
        return md_path
    return mt_terms_dir / f"{list_name}-metadata.txt"


def get_terms_glosses_path(list_name: str, iso: str = "en", mt_terms_dir: Path = SIL_NLP_ENV.mt_terms_dir) -> Path:
    iso = iso.lower()
    gl_path = SIL_NLP_ENV.assets_dir / f"{iso}-{list_name}-glosses.txt"
    if gl_path.is_file():
        return gl_path
    return mt_terms_dir / f"{iso}-{list_name}-glosses.txt"


def get_terms_vrefs_path(list_name: str, mt_terms_dir: Path = SIL_NLP_ENV.mt_terms_dir) -> Path:
    md_path = SIL_NLP_ENV.assets_dir / f"{list_name}-vrefs.txt"
    if md_path.is_file():
        return md_path
    return mt_terms_dir / f"{list_name}-vrefs.txt"


def get_terms_renderings_path(iso: str, project: str, mt_terms_dir: Path = SIL_NLP_ENV.mt_terms_dir) -> Optional[Path]:
    matches = list(mt_terms_dir.glob(f"{iso}-{project}-*-renderings.txt"))
    if len(matches) == 0:
        return None
    assert len(matches) == 1
    return matches[0]


def get_terms_list(terms_renderings_path: Path) -> str:
    name = terms_renderings_path.stem
    parts = name.split("-")
    project = parts[1]
    list_type = parts[2]
    list_name = list_type
    if list_type == "Project":
        list_name = project
    return list_name


@dataclass(frozen=True)
class Term:
    id: str
    cat: str
    domain: str
    glosses: List[str]
    renderings: List[str]
    vrefs: Set[VerseRef]


def get_terms(terms_renderings_path: Path, iso: str = "en") -> Dict[str, Term]:
    list_name = get_terms_list(terms_renderings_path)
    terms_metadata_path = get_terms_metadata_path(list_name)
    terms_glosses_path = get_terms_glosses_path(list_name, iso=iso)
    terms_vrefs_path = get_terms_vrefs_path(list_name)
    terms: Dict[str, Term] = {}
    terms_metadata = load_corpus(terms_metadata_path)
    terms_glosses = load_corpus(terms_glosses_path) if terms_glosses_path.is_file() else iter([])
    terms_renderings = load_corpus(terms_renderings_path)
    terms_vrefs = load_corpus(terms_vrefs_path) if terms_vrefs_path.is_file() else iter([])
    for metadata_line, glosses_line, renderings_line, vrefs_line in itertools.zip_longest(
        terms_metadata, terms_glosses, terms_renderings, terms_vrefs
    ):
        term_id, cat, domain = metadata_line.split("\t", maxsplit=3)
        glosses = [] if glosses_line is None or len(glosses_line) == 0 else glosses_line.split("\t")
        renderings = [] if len(renderings_line) == 0 else renderings_line.split("\t")
        vrefs = (
            set()
            if vrefs_line is None or len(vrefs_line) == 0
            else set(VerseRef.from_string(vref, ORIGINAL_VERSIFICATION) for vref in vrefs_line.split("\t"))
        )
        terms[term_id] = Term(term_id, cat, domain, glosses, renderings, vrefs)
    return terms


def get_terms_corpus(
    src_terms: Dict[str, Term],
    trg_terms: Dict[str, Term],
    cats: Optional[Set[str]],
    dictionary_books: Optional[Set[int]],
) -> pd.DataFrame:
    data: Set[Tuple[str, str, bool, str]] = set()
    for src_term in src_terms.values():
        if cats is not None and src_term.cat not in cats:
            continue

        trg_term = trg_terms.get(src_term.id)
        if trg_term is None:
            continue

        dictionary = dictionary_books is None or any(vref.book_num in dictionary_books for vref in src_term.vrefs)

        for src_rendering in src_term.renderings:
            for trg_rendering in trg_term.renderings:
                data.add((src_rendering, trg_rendering, dictionary, "\t".join(str(vref) for vref in src_term.vrefs)))
    return pd.DataFrame(data, columns=["source", "target", "dictionary", "vrefs"])


def get_terms_data_frame(
    terms: Dict[str, Term], cats: Optional[Set[str]], dictionary_books: Optional[Set[int]]
) -> pd.DataFrame:
    data: Set[Tuple[str, str, bool, str]] = set()
    for term in terms.values():
        if cats is not None and term.cat not in cats:
            continue

        dictionary = dictionary_books is None or any(vref.book_num in dictionary_books for vref in term.vrefs)

        for rendering in term.renderings:
            for gloss in term.glosses:
                data.add((rendering, gloss, dictionary, "\t".join(str(vref) for vref in term.vrefs)))
    return pd.DataFrame(data, columns=["rendering", "gloss", "dictionary", "vrefs"])


def count_lines(file_path: Path, line_filter: Callable[[str], bool] = lambda l: True) -> int:
    with file_path.open("r", encoding="utf-8-sig") as file:
        return sum(1 for l in file if line_filter(l))
