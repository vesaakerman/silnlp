import os
import random
from glob import glob
from typing import List, Optional, Set, Type, Union

from ..common.environment import PT_PREPROCESSED_DIR
from ..common.utils import is_set
from .config import Config, DataFileType
from .noise import DeleteRandomToken, NoiseMethod, RandomTokenPermutation, ReplaceRandomToken
from .utils import decode_sp, encode_sp


def parse_iso(file_path: str) -> str:
    file_name = os.path.basename(file_path)
    index = file_name.index("-")
    return file_name[:index]


class CorpusPair:
    def __init__(
        self,
        src_file_path: str,
        trg_file_path: str,
        type: DataFileType,
        src_noise: List[NoiseMethod],
        size: Union[float, int],
        test_size: Optional[Union[float, int]],
        val_size: Optional[Union[float, int]],
    ) -> None:
        self.src_file_path = src_file_path
        self.src_iso = parse_iso(src_file_path)
        self.trg_file_path = trg_file_path
        self.trg_iso = parse_iso(trg_file_path)
        self.type = type
        self.src_noise = src_noise
        self.size = size
        self.test_size = test_size
        self.val_size = val_size

    @property
    def is_train(self):
        return is_set(self.type, DataFileType.TRAIN)

    @property
    def is_test(self):
        return is_set(self.type, DataFileType.TEST)

    @property
    def is_val(self):
        return is_set(self.type, DataFileType.VAL)


def create_noise_methods(params: List[dict]) -> List[NoiseMethod]:
    if params is None:
        return None
    methods: List[NoiseMethod] = []
    for module in params:
        noise_type, args = next(iter(module.items()))
        if not isinstance(args, list):
            args = [args]
        noise_type = noise_type.lower()
        noise_method_class: Type[NoiseMethod]
        if noise_type == "dropout":
            noise_method_class = DeleteRandomToken
        elif noise_type == "replacement":
            noise_method_class = ReplaceRandomToken
        elif noise_type == "permutation":
            noise_method_class = RandomTokenPermutation
        else:
            raise ValueError("Invalid noise type: %s" % noise_type)
        methods.append(noise_method_class(*args))
    return methods


def parse_corpus_pairs(corpus_pairs: List[dict]) -> List[CorpusPair]:
    pairs: List[CorpusPair] = []
    for pair in corpus_pairs:
        type_strs: Optional[Union[str, List[str]]] = pair.get("type")
        if type_strs is None:
            type_strs = ["train", "test", "val"]
        elif isinstance(type_strs, str):
            type_strs = type_strs.split(",")
        type = DataFileType.NONE
        for type_str in type_strs:
            type_str = type_str.strip().lower()
            if type_str == "train":
                type |= DataFileType.TRAIN
            elif type_str == "test":
                type |= DataFileType.TEST
            elif type_str == "val":
                type |= DataFileType.VAL
        src: str = pair["src"]
        src_file_path = os.path.join(PT_PREPROCESSED_DIR, "data", f"{src}.txt")
        trg: str = pair["trg"]
        trg_file_path = os.path.join(PT_PREPROCESSED_DIR, "data", f"{trg}.txt")
        src_noise = create_noise_methods(pair.get("src_noise", []))
        size: Union[float, int] = pair.get("size", 1.0)
        test_size: Optional[Union[float, int]] = pair.get("test_size")
        if test_size is None and is_set(type, DataFileType.TRAIN | DataFileType.TEST):
            test_size = 250
        val_size: Optional[Union[float, int]] = pair.get("val_size")
        if val_size is None and is_set(type, DataFileType.TRAIN | DataFileType.VAL):
            val_size = 250
        pairs.append(CorpusPair(src_file_path, trg_file_path, type, src_noise, size, test_size, val_size))
    return pairs


def get_parallel_corpus_size(src_file_path: str, trg_file_path: str) -> int:
    count = 0
    with open(src_file_path, "r", encoding="utf-8") as src_file, open(trg_file_path, "r", encoding="utf-8") as trg_file:
        for src_line, trg_line in zip(src_file, trg_file):
            src_line = src_line.strip()
            trg_line = trg_line.strip()
            if len(src_line) > 0 and len(trg_line) > 0:
                count += 1
    return count


def split_corpus(corpus_size: int, split_size: Union[float, int], used_indices: Set[int] = set()) -> Optional[Set[int]]:
    if isinstance(split_size, float):
        split_size = int(split_size if split_size > 1 else corpus_size * split_size)
    population = (
        range(corpus_size) if len(used_indices) == 0 else [i for i in range(corpus_size) if i not in used_indices]
    )
    if split_size >= len(population):
        return None

    return set(random.sample(population, split_size))


class CorpusPairsConfig(Config):
    def __init__(self, exp_dir: str, config: dict) -> None:
        self.corpus_pairs = parse_corpus_pairs(config["data"]["corpus_pairs"])
        src_isos: Set[str] = set()
        trg_isos: Set[str] = set()
        src_file_paths: Set[str] = set()
        trg_file_paths: Set[str] = set()
        for pair in self.corpus_pairs:
            src_isos.add(pair.src_iso)
            trg_isos.add(pair.trg_iso)
            src_file_paths.add(pair.src_file_path)
            trg_file_paths.add(pair.trg_file_path)
        super().__init__(exp_dir, config, src_isos, trg_isos, src_file_paths, trg_file_paths)

    def _build_corpora(self, stats: bool) -> None:
        test_iso_pairs = set((p.src_iso, p.trg_iso) for p in self.corpus_pairs if p.is_test)
        src_spp, trg_spp = self.create_sp_processors()
        print("Writing data sets...")
        for old_file_path in glob(os.path.join(self.exp_dir, "test.*.txt")):
            os.remove(old_file_path)
        with open(
            os.path.join(self.exp_dir, "train.src.txt"), "w", encoding="utf-8", newline="\n"
        ) as train_src_file, open(
            os.path.join(self.exp_dir, "train.trg.txt"), "w", encoding="utf-8", newline="\n"
        ) as train_trg_file, open(
            os.path.join(self.exp_dir, "val.src.txt"), "w", encoding="utf-8", newline="\n"
        ) as val_src_file, open(
            os.path.join(self.exp_dir, "val.trg.txt"), "w", encoding="utf-8", newline="\n"
        ) as val_trg_file:
            for pair in self.corpus_pairs:
                corpus_size = get_parallel_corpus_size(pair.src_file_path, pair.trg_file_path)

                test_indices: Optional[Set[int]] = set()
                if pair.is_test:
                    test_size = pair.size if pair.test_size is None else pair.test_size
                    test_indices = split_corpus(corpus_size, test_size)

                val_indices: Optional[Set[int]] = set()
                if pair.is_val and test_indices is not None:
                    val_size = pair.size if pair.val_size is None else pair.val_size
                    val_indices = split_corpus(corpus_size, val_size, test_indices)

                train_indices: Optional[Set[int]] = set()
                if pair.is_train and test_indices is not None and val_indices is not None:
                    train_size = pair.size
                    train_indices = split_corpus(corpus_size, train_size, test_indices | val_indices)

                test_prefix = "test" if len(test_iso_pairs) == 1 else f"test.{pair.src_iso}.{pair.trg_iso}"
                with open(pair.src_file_path, "r", encoding="utf-8") as input_src_file, open(
                    pair.trg_file_path, "r", encoding="utf-8"
                ) as input_trg_file, open(
                    os.path.join(self.exp_dir, f"{test_prefix}.src.txt"), "a", encoding="utf-8", newline="\n"
                ) as test_src_file, open(
                    os.path.join(self.exp_dir, f"{test_prefix}.trg.detok.txt"), "a", encoding="utf-8", newline="\n"
                ) as test_trg_file:
                    index = 0
                    for src_line, trg_line in zip(input_src_file, input_trg_file):
                        src_line = src_line.strip()
                        trg_line = trg_line.strip()
                        if len(src_line) == 0 or len(trg_line) == 0:
                            continue

                        src_sentence = self._insert_trg_tag(pair.trg_iso, src_line)
                        trg_sentence = trg_line

                        mirror_src_sentence = self._insert_trg_tag(pair.src_iso, trg_line)
                        mirror_trg_sentence = src_line
                        mirror_src_spp = trg_spp
                        mirror_trg_spp = src_spp

                        if pair.is_test and (test_indices is None or index in test_indices):
                            test_src_file.write(encode_sp(src_spp, src_sentence) + "\n")
                            test_trg_file.write(decode_sp(encode_sp(trg_spp, trg_sentence)) + "\n")
                        elif pair.is_val and (val_indices is None or index in val_indices):
                            val_src_file.write(encode_sp(src_spp, src_sentence) + "\n")
                            val_trg_file.write(encode_sp(trg_spp, trg_sentence) + "\n")
                            if self.mirror:
                                val_src_file.write(encode_sp(mirror_src_spp, mirror_src_sentence) + "\n")
                                val_trg_file.write(encode_sp(mirror_trg_spp, mirror_trg_sentence) + "\n")
                        elif pair.is_train and (train_indices is None or index in train_indices):
                            train_src_file.write(encode_sp(src_spp, self._noise(pair.src_noise, src_sentence)) + "\n")
                            train_trg_file.write(encode_sp(trg_spp, trg_sentence) + "\n")
                            if self.mirror:
                                train_src_file.write(encode_sp(mirror_src_spp, mirror_src_sentence) + "\n")
                                train_trg_file.write(encode_sp(mirror_trg_spp, mirror_trg_sentence) + "\n")

                        index += 1

    def _insert_trg_tag(self, trg_iso: str, src_sentence: str) -> str:
        if self.write_trg_tag:
            src_sentence = f"<2{trg_iso}> " + src_sentence
        return src_sentence

    def _noise(self, src_noise: List[NoiseMethod], src_sentence: str) -> str:
        if len(src_noise) == 0:
            return src_sentence
        tokens = src_sentence.split()
        if self.write_trg_tag:
            tag = tokens[:1]
            tokens = tokens[1:]
        for noise_method in src_noise:
            tokens = noise_method(tokens)
        if self.write_trg_tag:
            tokens = tag + tokens
        return " ".join(tokens)
