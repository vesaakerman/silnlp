import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..common.utils import get_repo_dir
from .aligner import Aligner
from .lexicon import Lexicon


class MachineAligner(Aligner):
    def __init__(
        self,
        id: str,
        model_type: str,
        model_dir: Path,
        plugin_file_path: Optional[Path] = None,
        has_inverse_model: bool = True,
        threshold: float = 0.01,
        direct_model_prefix: str = "src_trg_invswm",
        params: Dict[str, Any] = {},
    ) -> None:
        super().__init__(id, model_dir)
        self.model_type = model_type
        self._plugin_file_path = plugin_file_path
        self._has_inverse_model = has_inverse_model
        self._threshold = threshold
        self._direct_model_prefix = direct_model_prefix
        self._params = params
        self._lowercase = False

    @property
    def has_inverse_model(self) -> bool:
        return self._has_inverse_model

    @property
    def lowercase(self) -> bool:
        return self._lowercase

    @lowercase.setter
    def lowercase(self, value: bool) -> None:
        self._lowercase = value

    def train(self, src_file_path: Path, trg_file_path: Path) -> None:
        direct_lex_path = self.model_dir / "lexicon.direct.txt"
        if direct_lex_path.is_file():
            direct_lex_path.unlink()
        inverse_lex_path = self.model_dir / "lexicon.inverse.txt"
        if inverse_lex_path.is_file():
            inverse_lex_path.unlink()
        self.model_dir.mkdir(exist_ok=True)
        shutil.copyfile(src_file_path, self.model_dir / "src.txt")
        shutil.copyfile(trg_file_path, self.model_dir / "trg.txt")
        self._train_alignment_model()

    def align(self, out_file_path: Path, sym_heuristic: str = "grow-diag-final-and") -> None:
        self._align_parallel_corpus(
            self.model_dir / "src.txt", self.model_dir / "trg.txt", out_file_path, sym_heuristic
        )

    def force_align(
        self, src_file_path: Path, trg_file_path: Path, out_file_path: Path, sym_heuristic: str = "grow-diag-final-and"
    ) -> None:
        self._align_parallel_corpus(src_file_path, trg_file_path, out_file_path, sym_heuristic)

    def extract_lexicon(self, out_file_path: Path) -> None:
        lexicon = self.get_direct_lexicon()
        if self._has_inverse_model:
            inverse_lexicon = self.get_inverse_lexicon()
            print("Symmetrizing lexicons...", end="", flush=True)
            lexicon = Lexicon.symmetrize(lexicon, inverse_lexicon)
            print(" done.")
        lexicon.write(out_file_path)

    def get_direct_lexicon(self, include_special_tokens: bool = False) -> Lexicon:
        direct_lex_path = self.model_dir / "lexicon.direct.txt"
        self._extract_lexicon("direct", direct_lex_path)
        return Lexicon.load(direct_lex_path, include_special_tokens)

    def get_inverse_lexicon(self, include_special_tokens: bool = False) -> Lexicon:
        if not self._has_inverse_model:
            raise RuntimeError("The aligner does not have an inverse model.")
        inverse_lex_path = self.model_dir / "lexicon.inverse.txt"
        self._extract_lexicon("inverse", inverse_lex_path)
        return Lexicon.load(inverse_lex_path, include_special_tokens)

    def _train_alignment_model(self) -> None:
        args: List[str] = [
            "dotnet",
            "machine",
            "train",
            "alignment-model",
            str(self.model_dir) + os.sep,
            str(self.model_dir / "src.txt"),
            str(self.model_dir / "trg.txt"),
            "-mt",
            self.model_type,
        ]
        if self._lowercase:
            args.append("-l")
        if self._plugin_file_path is not None:
            args.append("-mp")
            args.append(str(self._plugin_file_path))
        if len(self._params) > 0:
            args.append("-tp")
            for key, value in self._params.items():
                args.append(f"{key}={value}")
        subprocess.run(args, cwd=get_repo_dir())

    def _align_parallel_corpus(
        self, src_file_path: Path, trg_file_path: Path, output_file_path: Path, sym_heuristic: str
    ) -> None:
        args: List[str] = [
            "dotnet",
            "machine",
            "align",
            str(self.model_dir) + os.sep,
            str(src_file_path),
            str(trg_file_path),
            str(output_file_path),
            "-mt",
            self.model_type,
            "-sh",
            sym_heuristic,
        ]
        if self._lowercase:
            args.append("-l")
        if self._plugin_file_path is not None:
            args.append("-mp")
            args.append(str(self._plugin_file_path))
        subprocess.run(args, cwd=get_repo_dir())

    def _extract_lexicon(self, direction: str, out_file_path: Path) -> None:
        args: List[str] = [
            "dotnet",
            "machine",
            "extract-lexicon",
            str(self.model_dir),
            str(out_file_path),
            "-mt",
            self.model_type,
            "-p",
            "-ss",
            "-t",
            str(self._threshold),
            "-d",
            direction,
        ]
        if self._plugin_file_path is not None:
            args.append("-mp")
            args.append(str(self._plugin_file_path))
        subprocess.run(args, cwd=get_repo_dir())


class Ibm1MachineAligner(MachineAligner):
    def __init__(self, model_dir: Path) -> None:
        super().__init__("ibm1", "ibm1", model_dir)


class Ibm2MachineAligner(MachineAligner):
    def __init__(self, model_dir: Path) -> None:
        super().__init__("ibm2", "ibm2", model_dir)


class HmmMachineAligner(MachineAligner):
    def __init__(self, model_dir: Path) -> None:
        super().__init__("hmm", "hmm", model_dir)


class FastAlignMachineAligner(MachineAligner):
    def __init__(self, model_dir: Path) -> None:
        super().__init__("fast_align", "fast_align", model_dir)


class ParatextMachineAligner(MachineAligner):
    def __init__(self, model_dir: Path) -> None:
        super().__init__(
            "pt",
            "betainv",
            model_dir,
            plugin_file_path=Path(os.getenv("BETA_INV_PLUGIN_PATH", ".")),
            has_inverse_model=False,
            threshold=0,
            direct_model_prefix="src_trg",
        )
